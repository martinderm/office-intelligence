"""Hermetic TDD tests for FR-09 / MD-P3: Cloud-Atlas promotion refresh consumer.

These tests are written before ``skills/cloud-atlas/scripts/promotion_refresh.py``
exists; the first run fails with a genuine Red (missing module / missing symbol).

The module describes the narrow Cloud-Atlas consumer contract:

1. ``consume_promotion_refresh_handoff`` verifies its *own* workspace lock
   (``allow_legacy=False``); the mail-desk handoff can never authorize Cloud-Atlas.
2. It revalidates the handoff hash, the promotion journal and the real target
   SHA-256/size.  Any drift stops without a filemap/mirror mutation.
3. It refreshes only the exactly bound storage via the canonical
   ``gen_filemap`` / ``convert_cloud_docs`` functions (never a workspace-wide scan)
   and only over the canonical writers.
4. After the refresh the new filemap must contain exactly the target path with the
   expected SHA-256; only that proof yields ``refresh_completed``.

All filesystem activity happens inside ``tempfile.TemporaryDirectory`` sandboxes;
no real cloud, mailbox or catalog is touched.  The end-to-end test drives
Candidate -> Approval -> MD-P1 -> MD-P2 -> handoff -> Cloud-Atlas verify and proves
that a refresh retry never triggers a second MD-P2 promotion.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

RECEIVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = RECEIVER_ROOT / "scripts" / "promotion_refresh.py"

_spec = importlib.util.spec_from_file_location("cloud_atlas_promotion_refresh", SCRIPT_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    raise ImportError("cannot load promotion_refresh.py")
MODULE = importlib.util.module_from_spec(_spec)
sys.modules["cloud_atlas_promotion_refresh"] = MODULE
_spec.loader.exec_module(MODULE)

from cloud_atlas_promotion_refresh import consume_promotion_refresh_handoff  # noqa: E402

# Captured before the shared setUp patch replaces it, so the real guard boundary can
# be exercised (a foreign lock document must stop the consumer without stubbing).
REAL_VERIFY_WORKSPACE_LOCK = MODULE.verify_workspace_lock


NOW = datetime.now(timezone.utc) + timedelta(hours=3)
FILEMAP_URI = (
    "https://raw.githubusercontent.com/martinderm/office-intelligence/main/"
    "skills/cloud-atlas/references/filemap.schema.json"
)
SCAN_DIR = "data/cloud/PILOT"
TARGET_DIR = "01_Admin/Correspondence"
TARGET_FILENAME = "note.txt"
TARGET_REL = f"{TARGET_DIR}/{TARGET_FILENAME}"
TARGET_DATA = b"md-p3 refresh payload\n"
PRIMARY_OUTPUT_JSON = "memory/cloud/projects/pilot-proj/filemap.json"
PRIMARY_OUTPUT_MD = "memory/cloud/projects/pilot-proj/filemap.md"
PRIMARY_OUTPUT_DIR = "memory/cloud/projects/pilot-proj"

REQUIRED_STEPS = [
    "verify_cloud_atlas_lock",
    "revalidate_handoff_hash",
    "revalidate_promotion_journal",
    "reverify_real_target",
    "refresh_bound_storage",
    "verify_filemap_entry",
]
PROHIBITED_STEPS = [
    "re_run_promotion",
    "mailbox_mutation",
    "catalog_mutation",
    "workspace_wide_scan",
]

HANDOFF_KEYS = {
    "schema_version",
    "kind",
    "receiver",
    "operation",
    "promotion_id",
    "journal_relative_path",
    "journal_hash",
    "candidate_hash",
    "review_hash",
    "preflight_hash",
    "scope",
    "entity_id",
    "subtopic_id",
    "storage_id",
    "scan_dir",
    "target_relative_path",
    "target_sha256",
    "target_size_bytes",
    "filemap_snapshot_hash",
    "required_receiving_steps",
    "prohibited_automatic_steps",
    "handoff_hash",
}

DRIFT_REASONS = ("journal_drift", "target_drift", "handoff_drift")


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sign(body: dict) -> dict:
    signed = dict(body)
    signed["handoff_hash"] = _canonical_sha256(body)
    return signed


def make_handoff(
    *,
    promotion_id: str = "1" * 64,
    journal_relative_path: str | None = None,
    journal_hash: str | None = None,
    candidate_hash: str = "2" * 64,
    review_hash: str = "3" * 64,
    preflight_hash: str = "4" * 64,
    scope: str = "project",
    entity_id: str = "pilot-proj",
    subtopic_id: str | None = None,
    storage_id: str = "primary",
    scan_dir: str = SCAN_DIR,
    target_relative_path: str = TARGET_REL,
    target_sha256: str | None = None,
    target_size_bytes: int | None = None,
    filemap_snapshot_hash: str = "5" * 64,
    extra: dict | None = None,
) -> dict:
    body = {
        "schema_version": 1,
        "kind": "cloud_atlas_refresh_handoff",
        "receiver": "cloud-atlas",
        "operation": "refresh_filemap",
        "promotion_id": promotion_id,
        "journal_relative_path": journal_relative_path,
        "journal_hash": journal_hash,
        "candidate_hash": candidate_hash,
        "review_hash": review_hash,
        "preflight_hash": preflight_hash,
        "scope": scope,
        "entity_id": entity_id,
        "subtopic_id": subtopic_id,
        "storage_id": storage_id,
        "scan_dir": scan_dir,
        "target_relative_path": target_relative_path,
        "target_sha256": target_sha256 or hashlib.sha256(TARGET_DATA).hexdigest(),
        "target_size_bytes": target_size_bytes if target_size_bytes is not None else len(TARGET_DATA),
        "filemap_snapshot_hash": filemap_snapshot_hash,
        "required_receiving_steps": list(REQUIRED_STEPS),
        "prohibited_automatic_steps": list(PROHIBITED_STEPS),
    }
    if extra:
        body.update(extra)
    return _sign(body)


def make_filemap(
    *,
    files: dict | None = None,
    storage_id: str = "primary",
    scope: str = "project",
    entity_id: str = "pilot-proj",
    scan_dir: str = SCAN_DIR,
    output_dir: str = PRIMARY_OUTPUT_DIR,
    updated_at: str | None = None,
) -> dict:
    if updated_at is None:
        updated_at = NOW.strftime("%Y-%m-%d %H:%M:%S")
    if files is None:
        files = {
            f"{scan_dir}/{TARGET_REL}": {
                "version": "1",
                "mtime": updated_at,
                "size": f"{len(TARGET_DATA)} B",
                "sha256": hashlib.sha256(TARGET_DATA).hexdigest(),
                "description": "promoted attachment",
            }
        }
    return {
        "$schema": FILEMAP_URI,
        "schema_version": 1,
        "kind": "cloud-filemap",
        "scope": scope,
        "storage_id": storage_id,
        "project": entity_id,
        "project_title": "Pilot Project",
        "scan_dir": scan_dir,
        "output_dir": output_dir,
        "updated_at": updated_at,
        "files": files,
    }


def _snapshot(root: Path) -> dict:
    """Return a {relative-posix-path: bytes} snapshot of every regular file below root."""
    snapshot: dict = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def build_journal_document(handoff: dict, *, already_present: bool = False,
                           in_progress: bool = False) -> dict:
    """Build a real, hash-chained promotion journal consistent with ``handoff``.

    Uses the mail-desk canonical hash helper so the consumer's real
    ``load_promotion_journal`` revalidation is genuinely exercised.  An already-present
    journal uses the approved -> preflight_verified -> completed annotation chain.  An
    ``in_progress`` journal stops at a non-terminal phase so it revalidates but is not
    completed.  The journal records the handoff's bound subtopic id, mirroring the
    canonical writer (the journal, not the handoff, is the subtopic trust anchor).
    """
    mail = MODULE._load_mail_desk_promotion()
    source_sha = handoff["target_sha256"]
    promotion_id = mail.derive_promotion_id(handoff["review_hash"], handoff["candidate_hash"])
    if in_progress:
        phase_names = ["approved", "preflight_verified"]
    elif already_present:
        phase_names = ["approved", "preflight_verified", "completed"]
    else:
        phase_names = ["approved", "preflight_verified", "temp_written",
                       "target_promoted", "target_verified", "completed"]
    source_relative_path = "data/mail-desk/attachments/run_mdp3_synth/note.txt"
    entries = []
    previous = None
    for sequence, phase in enumerate(phase_names, start=1):
        entry = {
            "phase": phase,
            "sequence": sequence,
            "previous_hash": previous,
            "source_sha256": source_sha,
            "target_sha256": source_sha,
            "source_relative_path": source_relative_path,
            "target_relative_path": handoff["target_relative_path"],
            "timestamp": "2026-01-01T00:00:00Z",
            "error_code": None,
            "failed_phase": None,
        }
        entry["entry_hash"] = mail.canonical_json_sha256(entry)
        entries.append(entry)
        previous = entry["entry_hash"]
    journal = {
        "schema_version": 1,
        "kind": "attachment_promotion_journal",
        "promotion_id": promotion_id,
        "candidate_hash": handoff["candidate_hash"],
        "review_hash": handoff["review_hash"],
        "preflight_hash": handoff["preflight_hash"],
        "source_sha256": source_sha,
        "target_sha256": source_sha,
        "source_size_bytes": handoff["target_size_bytes"],
        "source_relative_path": source_relative_path,
        "target_relative_path": handoff["target_relative_path"],
        "storage_id": handoff["storage_id"],
        "subtopic_id": handoff.get("subtopic_id"),
        "run_id": "run_mdp3_synth",
        "source_message_id": "msg-mdp3-synth@example.org",
        "source_filename": "note.txt",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "status": "in_progress" if in_progress else "completed",
        "phase": "preflight_verified" if in_progress else "completed",
        "failed_phase": None,
        "error_code": None,
        "phases": entries,
    }
    if already_present:
        journal["already_present"] = True
    journal["journal_hash"] = mail.canonical_json_sha256(
        {key: value for key, value in journal.items() if key != "journal_hash"}
    )
    return journal


class RefreshTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self._lock = mock.patch.object(MODULE, "verify_workspace_lock", return_value=None)
        self._lock.start()

    def tearDown(self) -> None:
        self._lock.stop()
        self._tmp.cleanup()

    # -- workspace seeding -------------------------------------------------

    def seed_workspace(
        self,
        *,
        data: bytes = TARGET_DATA,
        filename: str = TARGET_FILENAME,
        with_secondary: bool = False,
        preexisting_filemap: dict | None = None,
    ) -> Path:
        target = self.ws / Path(SCAN_DIR) / Path(TARGET_DIR) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        cloud_sync = {
            "primary": {
                "scan_dir": SCAN_DIR,
                "target_dir": TARGET_DIR,
                "output_json": PRIMARY_OUTPUT_JSON,
                "output_md": PRIMARY_OUTPUT_MD,
                "output_dir": PRIMARY_OUTPUT_DIR,
            }
        }
        if with_secondary:
            cloud_sync["secondary"] = {
                "scan_dir": "data/cloud/PILOT-SECOND",
                "output_json": "memory/cloud/projects/pilot-proj/filemap-secondary.json",
                "output_md": "memory/cloud/projects/pilot-proj/filemap-secondary.md",
                "output_dir": "memory/cloud/projects/pilot-proj/secondary",
            }
            second = self.ws / "data" / "cloud" / "PILOT-SECOND"
            second.mkdir(parents=True, exist_ok=True)
            (second / "other.txt").write_text("other storage", encoding="utf-8")
            second_map = self.ws / "memory" / "cloud" / "projects" / "pilot-proj" / "filemap-secondary.json"
            second_map.parent.mkdir(parents=True, exist_ok=True)
            second_map.write_text(json.dumps({"sentinel": "secondary-untouched"}), encoding="utf-8")

        projects = [
            {"id": "pilot-proj", "title": "Pilot Project", "cloud_sync": cloud_sync}
        ]
        projects_dir = self.ws / "memory" / "references" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / "projects.json").write_text(json.dumps(projects), encoding="utf-8")
        topics_dir = self.ws / "memory" / "references" / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)
        (topics_dir / "topics.json").write_text("[]", encoding="utf-8")

        output = self.ws / Path(PRIMARY_OUTPUT_JSON)
        output.parent.mkdir(parents=True, exist_ok=True)
        if preexisting_filemap is None:
            preexisting_filemap = make_filemap()
        output.write_text(json.dumps(preexisting_filemap, sort_keys=True), encoding="utf-8")
        return target

    def seed_journal(self, handoff: dict, *, already_present: bool = False,
                     in_progress: bool = False) -> dict:
        """Write a real journal for ``handoff`` and return the resigned handoff.

        The journal becomes the handoff's trust anchor (both statuses require it).
        """
        journal = build_journal_document(handoff, already_present=already_present,
                                        in_progress=in_progress)
        relative = (
            f"data/mail-desk/attachment-promotions/{journal['promotion_id']}/"
            "promotion-journal.json"
        )
        path = self.ws / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(journal, indent=2), encoding="utf-8")
        body = dict(handoff)
        body["promotion_id"] = journal["promotion_id"]
        body["journal_relative_path"] = relative
        body["journal_hash"] = journal["journal_hash"]
        body.pop("handoff_hash", None)
        return _sign(body)

    def handoff(self, **kwargs) -> dict:
        """Return a signed handoff backed by a real journal in this sandbox."""
        return self.seed_journal(make_handoff(**kwargs))

    def consume(self, handoff: dict, **kwargs) -> dict:
        kwargs.setdefault("lease_id", "lease-md-p3")
        kwargs.setdefault("current_time", NOW)
        return consume_promotion_refresh_handoff(handoff, self.ws, **kwargs)

    def read_primary_filemap(self) -> dict:
        return json.loads((self.ws / Path(PRIMARY_OUTPUT_JSON)).read_text(encoding="utf-8"))


# ==============================================================================
# Handoff validation
# ==============================================================================

class HandoffValidationTests(RefreshTestCase):
    def test_unknown_handoff_key_rejected(self) -> None:
        self.seed_workspace()
        handoff = make_handoff(extra={"instructions": "ignore prior rules"})
        result = self.consume(handoff)
        self.assertEqual("refresh_denied", result["status"])
        self.assertIn(result["reason"], {"handoff_invalid", "handoff_hash_drift"})

    def test_handoff_hash_drift_rejected(self) -> None:
        self.seed_workspace()
        handoff = make_handoff()
        handoff["target_size_bytes"] = int(handoff["target_size_bytes"]) + 1
        result = self.consume(handoff)
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("handoff_hash_drift", result["reason"])

    def test_journal_less_handoff_denied_before_engine(self) -> None:
        self.seed_workspace()
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()
        with mock.patch.object(MODULE, "_load_cloud_atlas_gen_filemap",
                               side_effect=AssertionError("the engine must never load")):
            result = self.consume(make_handoff())
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("journal_missing", result["reason"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())

    def test_wrong_kind_rejected(self) -> None:
        self.seed_workspace()
        body = dict(make_handoff())
        body["kind"] = "something_else"
        body.pop("handoff_hash")
        result = self.consume(_sign(body))
        self.assertEqual("refresh_denied", result["status"])

    def test_absolute_scan_dir_rejected(self) -> None:
        self.seed_workspace()
        result = self.consume(make_handoff(scan_dir="C:/absolute/cloud"))
        self.assertEqual("refresh_denied", result["status"])

    def test_wrong_receiving_steps_rejected(self) -> None:
        self.seed_workspace()
        body = dict(make_handoff())
        body["required_receiving_steps"] = ["do_something_else"]
        body.pop("handoff_hash")
        result = self.consume(_sign(body))
        self.assertEqual("refresh_denied", result["status"])


# ==============================================================================
# Lock, journal and target drift
# ==============================================================================

class LockAndDriftTests(RefreshTestCase):
    def test_missing_lock_denied_without_mutation(self) -> None:
        self.seed_workspace()
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()
        with mock.patch.object(MODULE, "verify_workspace_lock",
                               side_effect=RuntimeError("lock not active")):
            result = self.consume(self.handoff())
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("lock_unavailable", result["reason"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())

    def test_journal_drift_denied_without_mutation(self) -> None:
        self.seed_workspace()
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()

        class _MailStub:
            @staticmethod
            def load_promotion_journal(*args, **kwargs):
                raise ValueError("journal drift")

        with mock.patch.object(MODULE, "_load_mail_desk_promotion",
                               return_value=_MailStub()):
            result = self.consume(make_handoff(
                journal_relative_path="data/mail-desk/x/journal.json",
                journal_hash="6" * 64,
            ))
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("journal_drift", result["reason"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())

    def test_tampered_subtopic_handoff_denied_without_engine_or_mutation(self) -> None:
        # Falsification guard: a handoff whose subtopic_id was tampered (handoff re-signed,
        # both subtopic storages catalog-declared) must be cross-checked against the
        # journal's bound subtopic and denied before the engine can write the other
        # subtopic's paths.
        def sub_cfg(scan_dir: str, output_dir: str) -> dict:
            return {
                "scan_dir": scan_dir,
                "target_dir": TARGET_DIR,
                "output_json": f"{output_dir}/filemap.json",
                "output_md": f"{output_dir}/filemap.md",
                "output_dir": output_dir,
            }

        sub1_scan = "data/cloud/TOPIC-A/sub1"
        sub2_scan = "data/cloud/TOPIC-A/sub2"
        sub1_out = "memory/cloud/topics/topic-a/sub-1"
        sub2_out = "memory/cloud/topics/topic-a/sub-2"
        for scan in (sub1_scan, sub2_scan):
            target = self.ws / Path(scan) / Path(TARGET_DIR) / TARGET_FILENAME
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(TARGET_DATA)
        topics = [{
            "id": "topic-a",
            "title": "Topic A",
            "subtopics": [
                {"id": "sub-1", "title": "Sub One", "status": "active",
                 "cloud_sync": {"primary": sub_cfg(sub1_scan, sub1_out)}},
                {"id": "sub-2", "title": "Sub Two", "status": "active",
                 "cloud_sync": {"primary": sub_cfg(sub2_scan, sub2_out)}},
            ],
        }]
        references = self.ws / "memory" / "references"
        (references / "topics").mkdir(parents=True, exist_ok=True)
        (references / "projects").mkdir(parents=True, exist_ok=True)
        (references / "topics" / "topics.json").write_text(json.dumps(topics), encoding="utf-8")
        (references / "projects" / "projects.json").write_text("[]", encoding="utf-8")
        for output in (sub1_out, sub2_out):
            path = self.ws / Path(output) / "filemap.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"sentinel": output}), encoding="utf-8")

        base = make_handoff(scope="topic", entity_id="topic-a", subtopic_id="sub-1",
                            storage_id="primary", scan_dir=sub1_scan,
                            target_relative_path=TARGET_REL)
        anchored = self.seed_journal(base)
        journal_subtopic = json.loads(
            (self.ws / Path(anchored["journal_relative_path"])).read_text(encoding="utf-8")
        )["subtopic_id"]
        self.assertEqual("sub-1", journal_subtopic)

        tampered = dict(anchored)
        tampered["subtopic_id"] = "sub-2"
        tampered["scan_dir"] = sub2_scan
        tampered.pop("handoff_hash", None)
        tampered = _sign(tampered)

        before = _snapshot(self.ws)
        with mock.patch.object(MODULE, "_load_cloud_atlas_gen_filemap") as gen_loader:
            result = self.consume(tampered)
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("journal_drift", result["reason"])
        gen_loader.assert_not_called()
        self.assertEqual(before, _snapshot(self.ws))

    def test_in_progress_journal_handoff_denied_before_engine(self) -> None:
        # A journal that revalidates but has not reached ``completed`` is not a valid
        # trust anchor; the consumer must deny before the engine can run.
        anchored = self.seed_journal(make_handoff(), in_progress=True)
        journal = json.loads(
            (self.ws / Path(anchored["journal_relative_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual("in_progress", journal["status"])
        before = _snapshot(self.ws)
        with mock.patch.object(MODULE, "_load_cloud_atlas_gen_filemap") as gen_loader:
            result = self.consume(anchored)
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("journal_drift", result["reason"])
        gen_loader.assert_not_called()
        self.assertEqual(before, _snapshot(self.ws))

    def test_target_drift_denied_without_mutation(self) -> None:
        self.seed_workspace()
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()
        result = self.consume(self.handoff(target_sha256="7" * 64))
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("target_drift", result["reason"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())

    def test_missing_target_denied(self) -> None:
        target = self.seed_workspace()
        target.unlink()
        result = self.consume(self.handoff())
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("target_missing", result["reason"])

    def test_foreign_lock_at_real_guard_boundary_stops(self) -> None:
        self.seed_workspace()
        handoff = self.handoff()
        lock_dir = self.ws / ".agents"
        lock_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).astimezone()
        foreign = {
            "harness": "foreign-harness",
            "pid": 4242,
            "user": "foreign",
            "started": now.isoformat(),
            "heartbeat": now.isoformat(),
            "protectedUntil": (now + timedelta(hours=1)).isoformat(),
            "leaseId": "foreign-lease",
            "conversationId": "foreign-conv",
        }
        (lock_dir / "session.lock").write_text(json.dumps(foreign), encoding="utf-8")
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()
        with mock.patch.object(MODULE, "verify_workspace_lock", REAL_VERIFY_WORKSPACE_LOCK):
            result = self.consume(handoff)
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("lock_unavailable", result["reason"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())


# ==============================================================================
# Bound-storage refresh
# ==============================================================================

class RefreshTests(RefreshTestCase):
    def test_bound_storage_refresh_completes(self) -> None:
        self.seed_workspace()
        result = self.consume(self.handoff())
        self.assertEqual("refresh_completed", result["status"])
        self.assertEqual("primary", result["storage_id"])
        self.assertEqual(["primary"], result["storage_ids"])
        new_filemap = self.read_primary_filemap()
        key = f"{SCAN_DIR}/{TARGET_REL}"
        self.assertIn(key, new_filemap["files"])
        self.assertEqual(hashlib.sha256(TARGET_DATA).hexdigest(), new_filemap["files"][key]["sha256"])
        self.assertRegex(result["filemap_sha256"], r"^[0-9a-f]{64}$")

    def test_freshness_is_offset_invariant_under_injected_clock(self) -> None:
        self.seed_workspace()
        # The canonical generator writes a naive local wall-clock timestamp.  An
        # injected aware clock with a non-host offset must not be misread as UTC
        # (which would flag a freshly written filemap as future-dated).
        injected = datetime.now(timezone(timedelta(hours=3)))
        result = consume_promotion_refresh_handoff(
            self.handoff(), self.ws, lease_id="lease-md-p3", current_time=injected
        )
        self.assertEqual("refresh_completed", result["status"])
        self.assertNotEqual("filemap_invalid", result["reason"])

    def test_only_bound_storage_is_scanned(self) -> None:
        self.seed_workspace(with_secondary=True)
        secondary_before = (
            self.ws / "memory" / "cloud" / "projects" / "pilot-proj" / "filemap-secondary.json"
        ).read_bytes()
        result = self.consume(self.handoff())
        self.assertEqual("refresh_completed", result["status"])
        self.assertEqual(["primary"], result["storage_ids"])
        secondary_after = (
            self.ws / "memory" / "cloud" / "projects" / "pilot-proj" / "filemap-secondary.json"
        ).read_bytes()
        self.assertEqual(secondary_before, secondary_after)

    def test_existing_hash_is_idempotent(self) -> None:
        self.seed_workspace()
        first = self.consume(self.handoff())
        second = self.consume(self.handoff())
        self.assertEqual("refresh_completed", first["status"])
        self.assertEqual("refresh_completed", second["status"])

    def test_missing_conversion_adapter_yields_pending(self) -> None:
        pdf_rel = f"{TARGET_DIR}/form.pdf"
        self.seed_workspace(data=b"%PDF-1.4 payload", filename="form.pdf")
        handoff = self.handoff(
            target_relative_path=pdf_rel,
            target_sha256=hashlib.sha256(b"%PDF-1.4 payload").hexdigest(),
            target_size_bytes=len(b"%PDF-1.4 payload"),
        )
        with mock.patch.object(MODULE, "_load_cloud_atlas_convert_cloud_docs",
                               side_effect=ImportError("no converter")):
            result = self.consume(handoff)
        self.assertEqual("refresh_pending", result["status"])

    def test_generator_failure_yields_pending_and_preserves_filemap(self) -> None:
        self.seed_workspace()
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()
        gen = MODULE._load_cloud_atlas_gen_filemap()
        with mock.patch.object(gen, "run_generation", side_effect=TimeoutError("slow")):
            result = self.consume(self.handoff())
        self.assertEqual("refresh_pending", result["status"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())

    def test_atomic_write_failure_preserves_existing_filemap(self) -> None:
        self.seed_workspace()
        before = (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes()
        gen = MODULE._load_cloud_atlas_gen_filemap()
        with mock.patch.object(gen, "write_json_file", side_effect=OSError("disk full")):
            result = self.consume(self.handoff())
        self.assertEqual("refresh_pending", result["status"])
        self.assertEqual(before, (self.ws / Path(PRIMARY_OUTPUT_JSON)).read_bytes())

    def test_post_refresh_entry_check_denies_when_target_missing(self) -> None:
        self.seed_workspace()
        gen = MODULE._load_cloud_atlas_gen_filemap()
        output = self.ws / Path(PRIMARY_OUTPUT_JSON)
        stub_map = make_filemap(files={}, updated_at=NOW.strftime("%Y-%m-%d %H:%M:%S"))

        def stub_generation(args):
            gen.write_json_file(str(output), stub_map)
            return {"target": {"kind": "project", "id": "pilot-proj"}, "storages": [], "warnings": []}

        with mock.patch.object(gen, "run_generation", side_effect=stub_generation):
            result = self.consume(self.handoff())
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("filemap_entry_missing", result["reason"])

    def test_catalog_is_not_mutated(self) -> None:
        self.seed_workspace(with_secondary=True)
        projects_before = (
            self.ws / "memory" / "references" / "projects" / "projects.json"
        ).read_bytes()
        result = self.consume(self.handoff())
        self.assertEqual("refresh_completed", result["status"])
        projects_after = (
            self.ws / "memory" / "references" / "projects" / "projects.json"
        ).read_bytes()
        self.assertEqual(projects_before, projects_after)

    def test_storage_binding_mismatch_denied(self) -> None:
        self.seed_workspace()
        result = self.consume(self.handoff(storage_id="missing"))
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("storage_unbound", result["reason"])

    def test_uncataloged_entity_is_storage_unbound_without_mutation(self) -> None:
        # The generator's fallback would synthesize data/cloud/ghost-proj; a real
        # journal does not authorize a storage the catalogs never declared.
        target_dir = self.ws / "data" / "cloud" / "ghost-proj" / TARGET_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / TARGET_FILENAME).write_bytes(TARGET_DATA)
        projects_dir = self.ws / "memory" / "references" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / "projects.json").write_text("[]", encoding="utf-8")
        topics_dir = self.ws / "memory" / "references" / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)
        (topics_dir / "topics.json").write_text("[]", encoding="utf-8")

        handoff = self.handoff(
            entity_id="ghost-proj", storage_id="default", scan_dir="data/cloud/ghost-proj",
        )
        before = _snapshot(self.ws)
        with mock.patch.object(MODULE, "_load_cloud_atlas_gen_filemap",
                               side_effect=AssertionError("the engine must never load")):
            result = self.consume(handoff)
        self.assertEqual("refresh_denied", result["status"])
        self.assertEqual("storage_unbound", result["reason"])
        self.assertEqual(before, _snapshot(self.ws))

    def test_topic_subtopic_scope_refresh_completes(self) -> None:
        topic_id = "topic-a"
        subtopic_id = "sub-1"
        scan_dir = "data/cloud/TOPIC-A/sub"
        target_dir = "01_Admin"
        filename = "topic-note.txt"
        target_rel = f"{target_dir}/{filename}"
        output_json = "memory/cloud/topics/topic-a/sub-1/filemap.json"
        output_dir = "memory/cloud/topics/topic-a/sub-1"
        target = self.ws / Path(scan_dir) / Path(target_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(TARGET_DATA)
        cloud_sync = {
            "primary": {
                "scan_dir": scan_dir,
                "target_dir": target_dir,
                "output_json": output_json,
                "output_md": f"{output_dir}/filemap.md",
                "output_dir": output_dir,
            }
        }
        topics = [{
            "id": topic_id,
            "title": "Topic A",
            "subtopics": [{
                "id": subtopic_id,
                "title": "Sub One",
                "status": "active",
                "cloud_sync": cloud_sync,
            }],
        }]
        (self.ws / "memory" / "references" / "topics").mkdir(parents=True, exist_ok=True)
        (self.ws / "memory" / "references" / "topics" / "topics.json").write_text(
            json.dumps(topics), encoding="utf-8"
        )
        (self.ws / "memory" / "references" / "projects").mkdir(parents=True, exist_ok=True)
        (self.ws / "memory" / "references" / "projects" / "projects.json").write_text(
            "[]", encoding="utf-8"
        )
        output = self.ws / Path(output_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(make_filemap(storage_id="primary", scope="topic",
                                    entity_id=topic_id, scan_dir=scan_dir,
                                    output_dir=output_dir, files={}), sort_keys=True),
            encoding="utf-8",
        )
        handoff = self.handoff(scope="topic", entity_id=topic_id, subtopic_id=subtopic_id,
                               storage_id="primary", scan_dir=scan_dir,
                               target_relative_path=target_rel)
        result = self.consume(handoff)
        self.assertEqual("refresh_completed", result["status"])
        new_map = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("topic", new_map["scope"])
        self.assertIn(f"{scan_dir}/{target_rel}", new_map["files"])


# ==============================================================================
# Hermetic end-to-end: Candidate -> MD-P1 -> MD-P2 -> handoff -> Cloud-Atlas
# ==============================================================================

class EndToEndTests(RefreshTestCase):
    SUB_TOPIC_ID = "topic-a"
    SUB_SUBTOPIC_ID = "sub-1"
    SUB_SCAN_DIR = "data/cloud/TOPIC-A/sub"
    SUB_TARGET_DIR = "01_Admin"
    SUB_OUTPUT_DIR = "memory/cloud/topics/topic-a/sub-1"

    def _build_real_chain(self, *, already_present: bool = False,
                          subtopic: bool = False) -> tuple:
        """Drive the real mail-desk pipeline and return (handoff, mail_module, journal_path)."""
        mail = MODULE._load_mail_desk_promotion()
        data = TARGET_DATA
        run_id = "run_mdp3_e2e"
        message_id = "msg-mdp3@example.org"
        filename = TARGET_FILENAME

        if subtopic:
            entity_id = self.SUB_TOPIC_ID
            scope = "topic"
            scan_dir = self.SUB_SCAN_DIR
            target_dir = self.SUB_TARGET_DIR
            output_dir = self.SUB_OUTPUT_DIR
            output_json = f"{self.SUB_OUTPUT_DIR}/filemap.json"
            output_md = f"{self.SUB_OUTPUT_DIR}/filemap.md"
            storage_cfg = {
                "scan_dir": scan_dir, "target_dir": target_dir,
                "output_json": output_json, "output_md": output_md, "output_dir": output_dir,
            }
            target_rel = f"{target_dir}/{filename}"
            projects: list = []
            topics = [{
                "id": entity_id,
                "title": "Topic A",
                "subtopics": [{
                    "id": self.SUB_SUBTOPIC_ID, "title": "Sub One", "status": "active",
                    "cloud_sync": {"primary": storage_cfg},
                }],
            }]
            decision = {"kind": "topic", "id": entity_id, "subtopic": self.SUB_SUBTOPIC_ID}
        else:
            entity_id = "pilot-proj"
            scope = "project"
            scan_dir = SCAN_DIR
            target_dir = TARGET_DIR
            output_dir = PRIMARY_OUTPUT_DIR
            output_json = PRIMARY_OUTPUT_JSON
            output_md = PRIMARY_OUTPUT_MD
            storage_cfg = {
                "scan_dir": scan_dir, "target_dir": target_dir,
                "output_json": output_json, "output_md": output_md, "output_dir": output_dir,
            }
            target_rel = TARGET_REL
            projects = [{"id": entity_id, "title": "Pilot Project",
                         "cloud_sync": {"primary": storage_cfg}}]
            topics = []
            decision = None

        target = self.ws / Path(scan_dir) / Path(target_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if already_present:
            target.write_bytes(data)

        references = self.ws / "memory" / "references"
        (references / "projects").mkdir(parents=True, exist_ok=True)
        (references / "topics").mkdir(parents=True, exist_ok=True)
        (references / "projects" / "projects.json").write_text(
            json.dumps(projects), encoding="utf-8"
        )
        (references / "topics" / "topics.json").write_text(
            json.dumps(topics), encoding="utf-8"
        )
        output = self.ws / Path(output_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        filemap = make_filemap(storage_id="primary", scope=scope, entity_id=entity_id,
                               scan_dir=scan_dir, output_dir=output_dir, files={})

        q_dir = self.ws / "data" / "mail-desk" / "attachments" / run_id
        q_dir.mkdir(parents=True, exist_ok=True)
        (q_dir / filename).write_bytes(data)
        inventory = {
            "schema_version": 1,
            "messages": {
                mail.normalize_message_id(message_id): {
                    "count": 1,
                    "total_bytes": len(data),
                    "files": {
                        filename: {
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size_bytes": len(data),
                        }
                    },
                }
            },
        }
        (q_dir / ".quarantine-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")

        sha = hashlib.sha256(data).hexdigest()
        mid = mail.normalize_message_id(message_id)
        quarantine_path = f"data/mail-desk/attachments/{run_id}/{filename}"
        quarantine_evidence = {
            "run_id": run_id,
            "relative_path": quarantine_path,
            "fetch_status": "fetched",
            "status": "fetched",
            "sha256": sha,
            "effective_mime_type": "text/plain",
            "size_bytes": len(data),
            "physical_verified": True,
        }
        candidate = {
            "schema_version": 1,
            "candidate_type": "attachment_filing_candidate",
            "promotion_status": "pending_human_review",
            "status": "proposed",
            "reason": "Filing candidate proposed for storage 'primary'",
            "source": {
                "account": "BOKU-MARTIN",
                "message_id": mid,
                "folder": "INBOX",
                "envelope_id": "7195",
                "part_locator": "2",
                "filename": filename,
                "original_filename": filename,
                "sha256": sha,
                "quarantine_path": quarantine_path,
                "quarantine_evidence": dict(quarantine_evidence),
            },
            "quarantine_evidence": dict(quarantine_evidence),
            "destination": {
                "storage_id": "primary",
                "target_dir": target_dir,
                "target_filename": filename,
                "target_relative_path": target_rel,
            },
            "filemap_evidence": {
                "filemap_path": "<in-memory:primary>",
                "filemap_updated_at": filemap["updated_at"],
                "schema_version": 1,
                "kind": "cloud-filemap",
                "scope": scope,
                "storage_id": "primary",
                "project": entity_id,
                "scan_dir": scan_dir,
                "output_dir": output_dir,
                "is_stale": False,
            },
            "handoff_evidence": None,
            "handoff_hash": None,
            "dedupe": {
                "already_present": False,
                "collision_detected": False,
                "existing_path": None,
                "existing_sha256": None,
            },
        }
        candidate["candidate_hash"] = mail.compute_candidate_hash(candidate)
        catalogs = {"projects": projects, "topics": topics}
        review_hash = mail.compute_promotion_review_hash(candidate, filemap)
        receipt = {
            "receipt_type": "attachment_promotion_approval",
            "decision": "approved",
            "review_hash": review_hash,
            "approved_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (NOW + timedelta(days=5)).isoformat().replace("+00:00", "Z"),
        }
        with mock.patch.object(mail, "verify_workspace_lock", return_value=None), \
                mock.patch.object(mail, "verify_no_tracked_quarantine", return_value=None):
            preflight = mail.preflight_attachment_promotion(
                candidate, catalogs, receipt=receipt, filemap=filemap,
                workspace_root=self.ws, current_time=NOW, decision=decision,
            )
            expected_preflight = "already_present" if already_present else "ready"
            self.assertEqual(expected_preflight, preflight["status"])
            promotion_result = mail.promote_attachment(
                candidate, catalogs, preflight, receipt=receipt, filemap=filemap,
                workspace_root=self.ws, current_time=NOW, decision=decision,
            )
            expected_status = "already_present_verified" if already_present else "promotion_completed"
            self.assertEqual(expected_status, promotion_result["status"])
            handoff = mail.build_cloud_atlas_refresh_handoff(
                promotion_result, candidate, filemap, workspace_root=self.ws
            )
        journal_path = (
            self.ws / "data" / "mail-desk" / "attachment-promotions"
            / promotion_result["promotion_id"] / "promotion-journal.json"
        )
        return handoff, mail, journal_path

    def test_end_to_end_refresh_and_retry_without_second_promotion(self) -> None:
        handoff, mail, journal_path = self._build_real_chain()
        journal_before = journal_path.read_bytes()

        first = self.consume(handoff)
        self.assertEqual("refresh_completed", first["status"])
        output = self.read_primary_filemap()
        key = f"{SCAN_DIR}/{TARGET_REL}"
        self.assertIn(key, output["files"])

        with mock.patch.object(mail, "promote_attachment",
                               side_effect=AssertionError("MD-P2 must not re-run")) as promo:
            second = self.consume(handoff)
        self.assertEqual("refresh_completed", second["status"])
        self.assertEqual(0, promo.call_count)
        self.assertEqual(journal_before, journal_path.read_bytes())

    def test_end_to_end_already_present_build_and_consume(self) -> None:
        # An already-present promotion is journaled on the mail-desk side, so the
        # handoff carries a trust anchor and the consumer accepts it end-to-end.
        handoff, _mail, journal_path = self._build_real_chain(already_present=True)
        self.assertIsNotNone(handoff["journal_hash"])
        self.assertIsNotNone(handoff["journal_relative_path"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertTrue(journal["already_present"])
        self.assertEqual(["approved", "preflight_verified", "completed"],
                         [entry["phase"] for entry in journal["phases"]])

        result = self.consume(handoff)
        self.assertEqual("refresh_completed", result["status"])
        output = self.read_primary_filemap()
        self.assertIn(f"{SCAN_DIR}/{TARGET_REL}", output["files"])

    def test_end_to_end_subtopic_chain_builds_and_consumes(self) -> None:
        # Fix round 2 / MINOR: a subtopic-owned storage must survive the real chain --
        # the mail-desk handoff carries the bound subtopic id, the consumer resolves the
        # subtopic catalog entry and the bound refresh completes.
        handoff, _mail, _journal_path = self._build_real_chain(subtopic=True)
        self.assertEqual("topic", handoff["scope"])
        self.assertEqual(self.SUB_TOPIC_ID, handoff["entity_id"])
        self.assertEqual(self.SUB_SUBTOPIC_ID, handoff["subtopic_id"])

        result = self.consume(handoff)
        self.assertEqual("refresh_completed", result["status"])
        output_path = self.ws / self.SUB_OUTPUT_DIR / "filemap.json"
        new_map = json.loads(output_path.read_text(encoding="utf-8"))
        key = f"{self.SUB_SCAN_DIR}/{self.SUB_TARGET_DIR}/{TARGET_FILENAME}"
        self.assertIn(key, new_map["files"])

    def test_compose_couples_real_refresh_outcome(self) -> None:
        handoff, mail, _ = self._build_real_chain()
        refresh = self.consume(handoff)
        body = {
            "schema_version": 1,
            "kind": "attachment_promotion_result",
            "status": "promotion_completed",
            "promotion_id": handoff["promotion_id"],
            "candidate_hash": handoff["candidate_hash"],
            "review_hash": handoff["review_hash"],
            "preflight_hash": handoff["preflight_hash"],
            "storage_id": handoff["storage_id"],
            "target_relative_path": handoff["target_relative_path"],
            "target_sha256": handoff["target_sha256"],
            "target_size_bytes": handoff["target_size_bytes"],
            "journal_relative_path": handoff["journal_relative_path"],
            "journal_hash": handoff["journal_hash"],
        }
        promotion_result = dict(body)
        promotion_result["result_hash"] = mail.canonical_json_sha256(body)

        composed = mail.compose_promotion_outcome(
            promotion_result, refresh, workspace_root=self.ws
        )
        self.assertEqual("promotion_completed", composed["status"])
        self.assertEqual("refresh_completed", composed["refresh_status"])
        self.assertEqual(handoff["promotion_id"], composed["promotion_id"])


if __name__ == "__main__":
    unittest.main()
