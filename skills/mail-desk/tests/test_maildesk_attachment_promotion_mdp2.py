"""Hermetic TDD tests for FR-09 / MD-P2: atomic no-clobber storage writer.

These tests are written before the MD-P2 writer API exists in
``core.attachment_promotion``; the first run fails with an ``ImportError`` for the
missing writer symbols.  That is the genuine Red gate.

The module describes the complete MD-P2 contract on top of the MD-P1 approval and
read-only preflight:

1. ``derive_promotion_id`` is deterministic from ``review_hash`` + ``candidate_hash``.
2. ``promote_attachment`` re-validates the MD-P1 preflight envelope (evidence, not
   authority), re-executes the full MD-P1 preflight immediately before the first
   write and transfers exactly one approved attachment into the already mounted,
   writable storage without ever clobbering an existing target.
3. The promotion journal (Schema 1, hash-chained phases, atomic writes) proves a
   unique reconcilable state after every simulated interrupt.
4. ``load_promotion_journal`` fails closed on corrupted, swapped, unknown, skipped
   or contradictory phase histories.

The whole module performs zero real cloud, mailbox or office access.  All filesystem
activity happens inside ``tempfile.TemporaryDirectory`` sandboxes; write paths are
probed; mailbox and cloud adapters are trapped.  Fixture builders are reused from
``test_maildesk_attachment_promotion_mdp1``.
"""

from __future__ import annotations

import builtins
import copy
from datetime import timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(MAIL_DESK_ROOT / "scripts"), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_maildesk_attachment_promotion_mdp1 as mdp1  # noqa: E402
from core import himalaya  # noqa: E402
from core.attachment_fetch import WorkspaceLockError  # noqa: E402
from core.attachment_filing import compute_candidate_hash  # noqa: E402
from core.common import normalize_message_id  # noqa: E402
from core import attachment_promotion as promotion  # noqa: E402
from core.attachment_promotion import (  # noqa: E402
    PromotionJournalError,
    derive_promotion_id,
    load_promotion_journal,
    preflight_attachment_promotion,
    promote_attachment,
)


NOW = mdp1.NOW
DEFAULT_DATA = mdp1.DEFAULT_DATA
DEFAULT_FILENAME = mdp1.DEFAULT_FILENAME
DEFAULT_RUN_ID = mdp1.DEFAULT_RUN_ID
DEFAULT_MESSAGE_ID = mdp1.DEFAULT_MESSAGE_ID
DEFAULT_SCAN_DIR = mdp1.DEFAULT_SCAN_DIR
DEFAULT_TARGET_DIR = mdp1.DEFAULT_TARGET_DIR
PROMOTION_DIR = Path("data") / "mail-desk" / "attachment-promotions"
JOURNAL_FILENAME = "promotion-journal.json"

ORDERED_PHASES = (
    "approved",
    "preflight_verified",
    "temp_written",
    "target_promoted",
    "target_verified",
    "source_cleanup_pending",
    "completed",
)
TERMINAL_PHASES = ("failed", "recovery_required")
ALL_PHASES = ORDERED_PHASES + TERMINAL_PHASES
END_STATES = {
    "promotion_completed",
    "already_present_verified",
    "source_cleanup_pending",
    "collision_detected",
    "recovery_required",
}


class AbortSimulation(Exception):
    """Raised by an injected fault hook to simulate a crash after a durable phase."""


def _hook_after(*phases: str):
    """Return a fault hook that raises once any listed phase has been persisted."""
    targets = set(phases)

    def hook(phase: str) -> None:
        if phase in targets:
            raise AbortSimulation(f"abort after {phase}")

    return hook


def _promotion_dir(ws: Path, promotion_id: str) -> Path:
    return ws / PROMOTION_DIR / promotion_id


def _journal_path(ws: Path, promotion_id: str) -> Path:
    return _promotion_dir(ws, promotion_id) / JOURNAL_FILENAME


def _find_journal(ws: Path) -> Path:
    matches = sorted((ws / PROMOTION_DIR).glob(f"*/{JOURNAL_FILENAME}"))
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one promotion journal, found {matches}")
    return matches[0]


def _read_journal(ws: Path) -> dict:
    return json.loads(_find_journal(ws).read_text(encoding="utf-8"))


def _inventory_path(ws: Path, run_id: str = DEFAULT_RUN_ID) -> Path:
    return ws / "data" / "mail-desk" / "attachments" / run_id / ".quarantine-inventory.json"


def _target_path(ws: Path, target_dir: str = DEFAULT_TARGET_DIR,
                 target_filename: str = DEFAULT_FILENAME) -> Path:
    return ws / Path(DEFAULT_SCAN_DIR) / Path(target_dir) / target_filename


def _src_sha(data: bytes = DEFAULT_DATA) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_target_temp(target_parent: Path, path) -> bool:
    """Return True for the writer's own sibling temp of the default target file.

    The parent is compared via ``resolve()`` because the workspace temp path may carry
    an 8.3 short component while the writer's resolved target does not.
    """
    candidate = Path(path)
    if not candidate.name.startswith(f".{DEFAULT_FILENAME}."):
        return False
    try:
        return candidate.parent.resolve() == target_parent.resolve()
    except OSError:
        return False


def _add_second_attachment(ws: Path, *, run_id: str = DEFAULT_RUN_ID,
                           filename: str = "other.pdf",
                           data: bytes = b"%PDF-1.4 second attachment") -> None:
    """Add a second, consistent quarantine file to the same run inventory."""
    q_dir = ws / "data" / "mail-desk" / "attachments" / run_id
    (q_dir / filename).write_bytes(data)
    inv_path = q_dir / ".quarantine-inventory.json"
    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    entry = inv["messages"][normalize_message_id(DEFAULT_MESSAGE_ID)]
    entry["files"][filename] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }
    entry["count"] = len(entry["files"])
    entry["total_bytes"] = sum(f["size_bytes"] for f in entry["files"].values())
    inv_path.write_text(json.dumps(inv), encoding="utf-8")


def _write_real_filemap(ws: Path, filemap: dict) -> Path:
    path = ws / "memory" / "cloud" / "projects" / "pilot-proj" / "filemap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(filemap, sort_keys=True), encoding="utf-8")
    return path


class PromotionEnv:
    """Container for one hermetic promotion fixture."""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def build_promotion_env(tmp: str, **kwargs) -> PromotionEnv:
    """Create workspace, candidate, receipt and a ``ready`` MD-P1 preflight envelope."""
    ws, filemap, catalogs, candidate = mdp1.build_env(tmp, **kwargs)
    receipt = mdp1.make_receipt(candidate, filemap)
    envelope = preflight_attachment_promotion(
        candidate, catalogs, receipt=receipt, filemap=filemap,
        workspace_root=ws, current_time=NOW,
    )
    return PromotionEnv(ws=ws, filemap=filemap, catalogs=catalogs, candidate=candidate,
                        receipt=receipt, preflight=envelope)


class PromotionTestCase(unittest.TestCase):
    """Shared lock/guard patching and promotion invocation helpers."""

    def setUp(self) -> None:
        self.maxDiff = None
        self._lock_patcher = patch.object(promotion, "verify_workspace_lock", return_value=None)
        self._lock_patcher.start()
        self._tracked_patcher = patch.object(promotion, "verify_no_tracked_quarantine",
                                             return_value=None)
        self._tracked_patcher.start()

    def tearDown(self) -> None:
        self._tracked_patcher.stop()
        self._lock_patcher.stop()

    def promote(self, env: PromotionEnv, *, preflight=None, current_time=NOW, **kwargs) -> dict:
        return promote_attachment(
            env.candidate, env.catalogs, preflight if preflight is not None else env.preflight,
            receipt=env.receipt, filemap=env.filemap, workspace_root=env.ws,
            current_time=current_time, **kwargs,
        )

    def assert_no_own_temp(self, ws: Path) -> None:
        target_parent = _target_path(ws).parent
        leftovers = [p.name for p in target_parent.glob("*.tmp")]
        self.assertEqual([], leftovers, "own temp files must be cleaned up")


# ==============================================================================
# Identity, phase vocabulary and result schema
# ==============================================================================

class PromotionIdentityTests(PromotionTestCase):
    def test_promotion_id_is_deterministic_sha256(self) -> None:
        first = derive_promotion_id("a" * 64, "b" * 64)
        second = derive_promotion_id("a" * 64, "b" * 64)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_promotion_id_binds_both_hashes(self) -> None:
        baseline = derive_promotion_id("a" * 64, "b" * 64)
        self.assertNotEqual(baseline, derive_promotion_id("c" * 64, "b" * 64))
        self.assertNotEqual(baseline, derive_promotion_id("a" * 64, "c" * 64))

    def test_journal_phase_vocabulary_is_exact(self) -> None:
        self.assertEqual(set(ALL_PHASES), set(promotion.JOURNAL_PHASES))

    def test_result_end_state_vocabulary_is_exact(self) -> None:
        self.assertEqual(END_STATES, set(promotion.PROMOTION_RESULT_STATUSES))

    def test_result_schema_shape_on_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            result = self.promote(env)
        self.assertEqual(1, result["schema_version"])
        self.assertEqual("attachment_promotion_result", result["kind"])
        for field in ("status", "reason", "error_code", "phase", "promotion_id",
                      "candidate_hash", "review_hash", "preflight_hash", "storage_id",
                      "target_relative_path", "target_sha256", "target_size_bytes",
                      "journal_relative_path", "journal_hash", "result_hash"):
            self.assertIn(field, result, msg=field)
        self.assertRegex(result["promotion_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(env.candidate["candidate_hash"], result["candidate_hash"])
        self.assertEqual(env.preflight["review_hash"], result["review_hash"])
        self.assertEqual("primary", result["storage_id"])
        self.assertEqual("01_Admin/Correspondence/minutes_2026.pdf", result["target_relative_path"])
        self.assertEqual(_src_sha(), result["target_sha256"])
        self.assertEqual(len(DEFAULT_DATA), result["target_size_bytes"])

    def test_result_contains_no_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            result = self.promote(env)
            serialized = json.dumps(result)
        self.assertNotIn(str(env.ws), serialized)
        self.assertNotIn("\\\\", serialized)

# ==============================================================================
# Happy path
# ==============================================================================

class PromotionHappyPathTests(PromotionTestCase):
    def test_happy_path_promotes_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            result = self.promote(env)
            target = _target_path(env.ws)
            self.assertEqual("promotion_completed", result["status"])
            self.assertEqual("completed", result["phase"])
            self.assertIsNone(result["error_code"])
            self.assertTrue(target.is_file())
            self.assertEqual(DEFAULT_DATA, target.read_bytes())
            self.assert_no_own_temp(env.ws)

    def test_journal_schema_and_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            result = self.promote(env)
            journal = _read_journal(env.ws)
        self.assertEqual(1, journal["schema_version"])
        self.assertEqual("attachment_promotion_journal", journal["kind"])
        self.assertEqual(result["promotion_id"], journal["promotion_id"])
        self.assertEqual(env.candidate["candidate_hash"], journal["candidate_hash"])
        self.assertEqual(env.preflight["review_hash"], journal["review_hash"])
        phases = [entry["phase"] for entry in journal["phases"]]
        self.assertEqual(
            ["approved", "preflight_verified", "temp_written", "target_promoted",
             "target_verified", "completed"],
            phases,
        )
        previous = None
        for entry in journal["phases"]:
            self.assertEqual(previous, entry["previous_hash"])
            self.assertRegex(entry["entry_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(env.candidate["source"]["sha256"], entry["source_sha256"])
            self.assertEqual(result["target_relative_path"], entry["target_relative_path"])
            previous = entry["entry_hash"]
        self.assertEqual(journal["journal_hash"], result["journal_hash"])
        self.assertEqual(result["journal_relative_path"],
                         f"data/mail-desk/attachment-promotions/{result['promotion_id']}/"
                         f"{JOURNAL_FILENAME}")

    def test_journal_phase_chain_is_recomputable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            journal = _read_journal(env.ws)
        for entry in journal["phases"]:
            body = {k: v for k, v in entry.items() if k != "entry_hash"}
            self.assertEqual(entry["entry_hash"], promotion.canonical_json_sha256(body))
        body = {k: v for k, v in journal.items() if k != "journal_hash"}
        self.assertEqual(journal["journal_hash"], promotion.canonical_json_sha256(body))

    def test_source_and_inventory_are_cleaned_after_durable_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            inv = json.loads(_inventory_path(env.ws).read_text(encoding="utf-8"))
            source_gone = not source.exists()
            message_gone = normalize_message_id(DEFAULT_MESSAGE_ID) not in inv["messages"]
        self.assertTrue(source_gone)
        self.assertTrue(message_gone)

    def test_other_run_attachments_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            _add_second_attachment(env.ws)
            self.promote(env)
            other = (env.ws / "data" / "mail-desk" / "attachments" / DEFAULT_RUN_ID / "other.pdf")
            inv = json.loads(_inventory_path(env.ws).read_text(encoding="utf-8"))
            entry = inv["messages"][normalize_message_id(DEFAULT_MESSAGE_ID)]
            other_exists = other.is_file()
            count = entry["count"]
            has_other = "other.pdf" in entry["files"]
        self.assertTrue(other_exists)
        self.assertEqual(1, count)
        self.assertTrue(has_other)

    def test_inventory_removal_is_atomic_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            _add_second_attachment(env.ws)
            self.promote(env)
            inv = json.loads(_inventory_path(env.ws).read_text(encoding="utf-8"))
        entry = inv["messages"][normalize_message_id(DEFAULT_MESSAGE_ID)]
        self.assertEqual(1, entry["count"])
        self.assertEqual(entry["count"], len(entry["files"]))

    def test_preflight_is_reexecuted_before_first_write(self) -> None:
        calls: list[str] = []
        real = promotion.preflight_attachment_promotion

        def counting(*args, **kwargs):
            calls.append("preflight")
            return real(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch.object(promotion, "preflight_attachment_promotion", side_effect=counting):
                result = self.promote(env)
        self.assertEqual("promotion_completed", result["status"])
        self.assertGreaterEqual(len(calls), 1)


# ==============================================================================
# Idempotency and collision
# ==============================================================================

class PromotionIdempotencyTests(PromotionTestCase):
    def test_target_already_present_same_hash_is_verified_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp, target_state="already_present")
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            before = _target_path(env.ws).read_bytes()
            result = self.promote(env)
            after = _target_path(env.ws).read_bytes()
            source_kept = source.exists()
        self.assertEqual("already_present_verified", result["status"])
        self.assertEqual(before, after)
        self.assertTrue(source_kept, "already-present verification must not remove evidence")

    def test_second_promotion_after_completion_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self.promote(env)
            link_calls: list[tuple] = []
            real_link = os.link

            def counting_link(src, dst, *args, **kwargs):
                link_calls.append((str(src), str(dst)))
                return real_link(src, dst, *args, **kwargs)

            with patch("os.link", side_effect=counting_link):
                second = self.promote(env)
        self.assertEqual("promotion_completed", first["status"])
        self.assertEqual("promotion_completed", second["status"])
        self.assertEqual([], link_calls, "a completed promotion must never link a second time")

    def test_retry_never_creates_a_second_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            target = _target_path(env.ws)
            first_stat = target.stat()
            self.promote(env)
            self.promote(env)
            second_stat = target.stat()
        self.assertEqual(first_stat.st_ino, second_stat.st_ino)

    def test_already_present_writes_no_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp, target_state="already_present")
            result = self.promote(env)
            journals = list((env.ws / PROMOTION_DIR).glob(f"*/{JOURNAL_FILENAME}"))
        self.assertEqual("already_present_verified", result["status"])
        self.assertEqual([], journals)
        self.assertIsNone(result["journal_relative_path"])


class PromotionCollisionTests(PromotionTestCase):
    def test_target_with_different_hash_is_collision_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp, target_state="collision")
            target = _target_path(env.ws)
            before = target.read_bytes()
            result = self.promote(env)
            after = target.read_bytes()
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            source_kept = source.exists()
            journals = list((env.ws / PROMOTION_DIR).glob(f"*/{JOURNAL_FILENAME}"))
        self.assertEqual("collision_detected", result["status"])
        self.assertEqual(before, after, "a collision must never overwrite the target")
        self.assertTrue(source_kept)
        self.assertEqual([], journals)

    def test_target_appearing_same_hash_between_preflight_and_write_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)
            real_link = os.link

            def racing_link(src, dst, *args, **kwargs):
                if not target.exists():
                    target.write_bytes(DEFAULT_DATA)
                return real_link(src, dst, *args, **kwargs)

            with patch("os.link", side_effect=racing_link):
                result = self.promote(env)
            after = target.read_bytes()
            leftovers = [p.name for p in target.parent.glob("*.tmp")]
            journal = _read_journal(env.ws)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            source_gone = not source.exists()
        # MINOR 2: the same-hash reconcile path advances the journal to completed
        # (target_promoted -> verify -> cleanup -> completed) instead of early-returning.
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual("completed", journal["phase"])
        self.assertEqual(DEFAULT_DATA, after)
        self.assertEqual([], leftovers)
        self.assertTrue(source_gone)

    def test_target_appearing_different_hash_between_preflight_and_write_is_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)
            real_link = os.link

            def racing_link(src, dst, *args, **kwargs):
                if not target.exists():
                    target.write_bytes(b"racing different bytes")
                return real_link(src, dst, *args, **kwargs)

            with patch("os.link", side_effect=racing_link):
                result = self.promote(env)
            after = target.read_bytes()
            leftovers = [p.name for p in target.parent.glob("*.tmp")]
        self.assertEqual("collision_detected", result["status"])
        self.assertEqual(b"racing different bytes", after)
        self.assertEqual([], leftovers)

    def test_eexist_from_link_is_honored_without_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)

            def eexist_link(src, dst, *args, **kwargs):
                target.write_bytes(DEFAULT_DATA)
                raise FileExistsError("exists")

            with patch("os.link", side_effect=eexist_link):
                result = self.promote(env)
            survived = target.read_bytes()
            journal = _read_journal(env.ws)
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual("completed", journal["phase"])
        self.assertEqual(DEFAULT_DATA, survived)
        self.assert_no_own_temp(env.ws)

# ==============================================================================
# Temp file, no-clobber primitive and target verification
# ==============================================================================

class PromotionTempFileTests(PromotionTestCase):
    def test_temp_is_written_exclusively_and_fsynced(self) -> None:
        fsync_paths: list[int] = []
        real_fsync = os.fsync

        def counting_fsync(fd):
            fsync_paths.append(fd)
            return real_fsync(fd)

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch("os.fsync", side_effect=counting_fsync):
                result = self.promote(env)
        self.assertEqual("promotion_completed", result["status"])
        self.assertGreaterEqual(len(fsync_paths), 1)

    def test_temp_exclusive_create_is_used(self) -> None:
        flags_seen: list[int] = []
        real_open = os.open

        def capturing_open(path, flags, *args, **kwargs):
            if str(path).endswith(".tmp"):
                flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch("os.open", side_effect=capturing_open):
                result = self.promote(env)
        self.assertEqual("promotion_completed", result["status"])
        self.assertTrue(any(flags & os.O_EXCL for flags in flags_seen))

    def test_temp_write_failure_fails_closed_and_cleans_own_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch("os.fdopen", side_effect=OSError("disk full")):
                result = self.promote(env)
            target = _target_path(env.ws)
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(target.exists())
        self.assert_no_own_temp(env.ws)

    def test_corrupted_temp_is_detected_before_promotion(self) -> None:
        real_fsync = os.fsync

        def truncating_fsync(fd):
            real_fsync(fd)
            try:
                os.ftruncate(fd, 1)
            except OSError:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch("os.fsync", side_effect=truncating_fsync):
                result = self.promote(env)
            target = _target_path(env.ws)
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertFalse(target.exists())


class PromotionNoClobberTests(PromotionTestCase):
    def test_link_oserror_fails_closed_without_replace_fallback(self) -> None:
        replace_destinations: list[str] = []
        real_replace = os.replace

        def recording_replace(src, dst, *args, **kwargs):
            replace_destinations.append(str(dst))
            return real_replace(src, dst, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)
            with patch("os.link", side_effect=OSError("hard links unsupported")):
                with patch("os.replace", side_effect=recording_replace):
                    result = self.promote(env)
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(target.exists())
        self.assertNotIn(str(target), replace_destinations,
                         "target promotion must never fall back to os.replace")
        self.assert_no_own_temp(env.ws)

    def test_target_promotion_never_uses_os_replace(self) -> None:
        replace_destinations: list[str] = []
        real_replace = os.replace

        def recording_replace(src, dst, *args, **kwargs):
            replace_destinations.append(str(Path(dst)))
            return real_replace(src, dst, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch("os.replace", side_effect=recording_replace):
                result = self.promote(env)
            target = str(_target_path(env.ws))
        self.assertEqual("promotion_completed", result["status"])
        self.assertNotIn(target, replace_destinations)


class PromotionTargetVerificationTests(PromotionTestCase):
    def test_target_is_reopened_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            result = self.promote(env)
            journal = _read_journal(env.ws)
        phases = [entry["phase"] for entry in journal["phases"]]
        self.assertIn("target_verified", phases)
        self.assertEqual("completed", result["phase"])
        self.assertEqual(_src_sha(), result["target_sha256"])

    def test_target_hash_drift_after_promotion_is_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)
            real_link = os.link

            def corrupting_link(src, dst, *args, **kwargs):
                real_link(src, dst, *args, **kwargs)
                target.write_bytes(b"corrupted after link")

            with patch("os.link", side_effect=corrupting_link):
                result = self.promote(env)
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertEqual("recovery_required", result["status"])


# ==============================================================================
# Preflight envelope authority / drift
# ==============================================================================

class PromotionPreflightRevalidationTests(PromotionTestCase):
    def test_tampered_preflight_envelope_hash_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            tampered = dict(env.preflight)
            tampered["storage_id"] = "evil"
            result = self.promote(env, preflight=tampered)
            target = _target_path(env.ws)
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(target.exists())

    def test_preflight_envelope_without_valid_hash_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            tampered = dict(env.preflight)
            tampered["preflight_hash"] = "0" * 64
            result = self.promote(env, preflight=tampered)
        self.assertEqual("recovery_required", result["status"])

    def test_rerun_preflight_catches_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            source.write_bytes(b"tampered on disk")
            result = self.promote(env)
            target = _target_path(env.ws)
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(target.exists())

    def test_rerun_preflight_catches_receipt_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            later = NOW + timedelta(days=30)
            result = self.promote(env, current_time=later)
        self.assertEqual("recovery_required", result["status"])

    def test_rerun_preflight_catches_catalog_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            drifted = copy.deepcopy(env.catalogs)
            drifted["projects"][0]["cloud_sync"] = {}
            result = promote_attachment(
                env.candidate, drifted, env.preflight, receipt=env.receipt,
                filemap=env.filemap, workspace_root=env.ws, current_time=NOW,
            )
        self.assertEqual("recovery_required", result["status"])

    def test_rerun_preflight_catches_filemap_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            stale = mdp1.make_filemap(updated_at="2026-09-10 09:30:00")
            result = promote_attachment(
                env.candidate, env.catalogs, env.preflight, receipt=env.receipt,
                filemap=stale, workspace_root=env.ws, current_time=NOW,
            )
        self.assertEqual("recovery_required", result["status"])

    def test_candidate_hash_drift_stops_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            drifted = copy.deepcopy(env.candidate)
            drifted["destination"]["target_relative_path"] = "tampered.pdf"
            result = promote_attachment(
                drifted, env.catalogs, env.preflight, receipt=env.receipt,
                filemap=env.filemap, workspace_root=env.ws, current_time=NOW,
            )
            target = _target_path(env.ws)
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(target.exists())

    def test_lock_failure_stops_before_write(self) -> None:
        self._lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env = build_promotion_env(tmp)
                with patch.object(promotion, "verify_workspace_lock",
                                  side_effect=WorkspaceLockError("no lock")):
                    result = self.promote(env)
                target = _target_path(env.ws)
            self.assertEqual("recovery_required", result["status"])
            self.assertFalse(target.exists())
        finally:
            self._lock_patcher.start()


# ==============================================================================
# Journal integrity
# ==============================================================================

class PromotionJournalIntegrityTests(PromotionTestCase):
    def _plant(self, ws: Path, promotion_id: str, journal: dict) -> Path:
        path = _journal_path(ws, promotion_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(journal, indent=2), encoding="utf-8")
        return path

    def test_corrupted_journal_json_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self.promote(env)
            path = _find_journal(env.ws)
            path.write_text("{not valid json", encoding="utf-8")
            second = self.promote(env)
        self.assertEqual("promotion_completed", first["status"])
        self.assertEqual("recovery_required", second["status"])

    def test_swapped_journal_binding_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            path = _find_journal(env.ws)
            journal = json.loads(path.read_text(encoding="utf-8"))
            journal["candidate_hash"] = "0" * 64
            path.write_text(json.dumps(journal), encoding="utf-8")
            reloaded = self.promote(env)
        self.assertEqual("recovery_required", reloaded["status"])

    def test_journal_hash_tamper_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            path = _find_journal(env.ws)
            journal = json.loads(path.read_text(encoding="utf-8"))
            journal["journal_hash"] = "0" * 64
            path.write_text(json.dumps(journal), encoding="utf-8")
            reloaded = self.promote(env)
        self.assertEqual("recovery_required", reloaded["status"])

    def test_broken_phase_chain_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            path = _find_journal(env.ws)
            journal = json.loads(path.read_text(encoding="utf-8"))
            journal["phases"][2]["previous_hash"] = "0" * 64
            path.write_text(json.dumps(journal), encoding="utf-8")
            reloaded = self.promote(env)
        self.assertEqual("recovery_required", reloaded["status"])

    def test_unknown_phase_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            path = _find_journal(env.ws)
            journal = json.loads(path.read_text(encoding="utf-8"))
            entry = journal["phases"][-1]
            entry["phase"] = "teleported"
            entry_body = {k: v for k, v in entry.items() if k != "entry_hash"}
            entry["entry_hash"] = promotion.canonical_json_sha256(entry_body)
            journal["phase"] = "teleported"
            path.write_text(json.dumps(journal), encoding="utf-8")
            with self.assertRaises(PromotionJournalError):
                load_promotion_journal(path)

    def test_skipped_phase_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            path = _find_journal(env.ws)
            journal = json.loads(path.read_text(encoding="utf-8"))
            # Remove preflight_verified to create an approved -> temp_written skip.
            phases = [p for p in journal["phases"] if p["phase"] != "preflight_verified"]
            # Rebuild the chain hashes after the removal.
            previous = None
            for entry in phases:
                entry["previous_hash"] = previous
                body = {k: v for k, v in entry.items() if k != "entry_hash"}
                entry["entry_hash"] = promotion.canonical_json_sha256(body)
                previous = entry["entry_hash"]
            journal["phases"] = phases
            journal["phase"] = phases[-1]["phase"]
            journal["journal_hash"] = promotion.canonical_json_sha256(
                {k: v for k, v in journal.items() if k != "journal_hash"}
            )
            path.write_text(json.dumps(journal), encoding="utf-8")
            with self.assertRaises(PromotionJournalError):
                load_promotion_journal(path)

    def test_completed_journal_returns_idempotent_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self.promote(env)
            journal = _read_journal(env.ws)
            second = self.promote(env)
        self.assertEqual("completed", journal["phase"])
        self.assertEqual(first["journal_hash"], second["journal_hash"])
        self.assertEqual("promotion_completed", second["status"])

    def test_journal_reload_validates_expected_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self.promote(env)
            path = _find_journal(env.ws)
            journal = load_promotion_journal(path)
        self.assertEqual(env.candidate["candidate_hash"], journal["candidate_hash"])
        self.assertEqual(env.preflight["review_hash"], journal["review_hash"])

# ==============================================================================
# Fault injection after every durable phase and retries
# ==============================================================================

class PromotionFaultInjectionTests(PromotionTestCase):
    ABORTABLE = ("approved", "preflight_verified", "temp_written",
                 "target_promoted", "target_verified", "completed")

    def test_abort_after_every_phase_leaves_unique_reconcilable_state(self) -> None:
        for phase in self.ABORTABLE:
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as tmp:
                    env = build_promotion_env(tmp)
                    with self.assertRaises(AbortSimulation):
                        self.promote(env, _fault_hook=_hook_after(phase))
                    journal = _read_journal(env.ws)
                    self.assertEqual(phase, journal["phase"])
                    if phase in ("target_promoted", "target_verified", "completed"):
                        self.assertTrue(_target_path(env.ws).is_file())
                    else:
                        self.assertFalse(_target_path(env.ws).is_file())

    def test_retry_after_every_phase_completes_with_single_target(self) -> None:
        fresh_link_phases = ("approved", "preflight_verified", "temp_written")
        for phase in self.ABORTABLE:
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as tmp:
                    env = build_promotion_env(tmp)
                    link_calls: list[str] = []
                    real_link = os.link

                    def counting_link(src, dst, *args, **kwargs):
                        link_calls.append(str(dst))
                        return real_link(src, dst, *args, **kwargs)

                    with self.assertRaises(AbortSimulation):
                        self.promote(env, _fault_hook=_hook_after(phase))
                    link_calls.clear()
                    with patch("os.link", side_effect=counting_link):
                        result = self.promote(env)
                    source = env.ws / Path(env.candidate["source"]["quarantine_path"])
                    target_exists = _target_path(env.ws).is_file()
                    source_gone = not source.exists()
                self.assertEqual("promotion_completed", result["status"])
                expected_links = 1 if phase in fresh_link_phases else 0
                self.assertEqual(expected_links, len(link_calls), msg=f"phase={phase}")
                self.assertTrue(target_exists)
                self.assertTrue(source_gone)

    def test_retry_after_completed_does_not_relink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with self.assertRaises(AbortSimulation):
                self.promote(env, _fault_hook=_hook_after("completed"))
            with patch("os.link", side_effect=AssertionError("must not relink")):
                result = self.promote(env)
        self.assertEqual("promotion_completed", result["status"])

    def test_partial_failure_after_temp_never_reports_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with self.assertRaises(AbortSimulation):
                self.promote(env, _fault_hook=_hook_after("temp_written"))
            journal = _read_journal(env.ws)
        self.assertNotEqual("completed", journal["phase"])
        self.assertEqual("temp_written", journal["phase"])

    def test_resume_after_target_promoted_reconciles_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with self.assertRaises(AbortSimulation):
                self.promote(env, _fault_hook=_hook_after("target_promoted"))
            target = _target_path(env.ws)
            self.assertTrue(target.is_file())
            result = self.promote(env)
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual(_src_sha(), result["target_sha256"])


# ==============================================================================
# Cleanup semantics
# ==============================================================================

class PromotionCleanupTests(PromotionTestCase):
    def test_source_unlink_failure_leaves_source_cleanup_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            real_unlink = Path.unlink

            def failing_unlink(self, *args, **kwargs):
                if Path(self).resolve() == Path(source).resolve():
                    raise PermissionError("denied")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                result = self.promote(env)
            target_exists = _target_path(env.ws).is_file()
        self.assertEqual("source_cleanup_pending", result["status"])
        self.assertEqual("source_cleanup_pending", result["phase"])
        self.assertTrue(target_exists)
        self.assertEqual(_src_sha(), result["target_sha256"])

    def test_inventory_update_failure_leaves_source_cleanup_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with patch.object(promotion, "_write_inventory_atomic", side_effect=OSError("disk")):
                result = self.promote(env)
            target_exists = _target_path(env.ws).is_file()
        self.assertEqual("source_cleanup_pending", result["status"])
        self.assertTrue(target_exists)

    def test_retry_from_source_cleanup_pending_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            real_unlink = Path.unlink

            def failing_unlink(self, *args, **kwargs):
                if Path(self).resolve() == Path(source).resolve():
                    raise PermissionError("denied")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                first = self.promote(env)
            second = self.promote(env)
            journal = _read_journal(env.ws)
            source_gone = not source.exists()
        self.assertEqual("source_cleanup_pending", first["status"])
        self.assertEqual("promotion_completed", second["status"])
        self.assertEqual("completed", journal["phase"])
        self.assertTrue(source_gone)

    def test_retry_from_source_cleanup_pending_never_relinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            real_unlink = Path.unlink

            def failing_unlink(self, *args, **kwargs):
                if Path(self).resolve() == Path(source).resolve():
                    raise PermissionError("denied")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                self.promote(env)
            with patch("os.link", side_effect=AssertionError("must not relink")):
                result = self.promote(env)
        self.assertEqual("promotion_completed", result["status"])

    def test_source_is_kept_when_target_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            target = _target_path(env.ws)
            real_link = os.link

            def corrupting_link(src, dst, *args, **kwargs):
                real_link(src, dst, *args, **kwargs)
                target.write_bytes(b"corrupted")

            with patch("os.link", side_effect=corrupting_link):
                result = self.promote(env)
            source_kept = source.exists()
        self.assertEqual("recovery_required", result["status"])
        self.assertTrue(source_kept, "source must survive when the target is unverified")


# ==============================================================================
# Zero unrelated-mutation proofs
# ==============================================================================

class WriterWriteProbe:
    """Record write primitives and trap mailbox access; everything else is real."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self._patchers: list[object] = []

    def __enter__(self) -> "WriterWriteProbe":
        probe = self
        real_replace = os.replace
        real_link = os.link
        real_builtin_open = builtins.open

        def rec_replace(src, dst, *args, **kwargs):
            probe.writes.append(("os.replace", str(dst)))
            return real_replace(src, dst, *args, **kwargs)

        def rec_link(src, dst, *args, **kwargs):
            probe.writes.append(("os.link", str(dst)))
            return real_link(src, dst, *args, **kwargs)

        def rec_open(file, mode="r", *args, **kwargs):
            if any(ch in str(mode) for ch in ("w", "a", "x", "+")):
                probe.writes.append(("builtins.open", str(file)))
            return real_builtin_open(file, mode, *args, **kwargs)

        self._patchers.append(patch("os.replace", rec_replace))
        self._patchers.append(patch("os.link", rec_link))
        self._patchers.append(patch("builtins.open", rec_open))
        self._patchers.append(patch.object(himalaya, "run_himalaya",
                                           side_effect=AssertionError("mailbox access")))
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        return None


class PromotionNoUnrelatedMutationTests(PromotionTestCase):
    def test_no_filemap_catalog_mailbox_or_cloud_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            filemap_file = _write_real_filemap(env.ws, env.filemap)
            before_filemap = filemap_file.read_bytes()
            before_catalogs = json.dumps(env.catalogs, sort_keys=True)
            with WriterWriteProbe() as probe:
                result = self.promote(env)
            after_filemap = filemap_file.read_bytes()
            after_catalogs = json.dumps(env.catalogs, sort_keys=True)
            filemap_str = str(filemap_file)
            forbidden = [dst for _, dst in probe.writes
                         if filemap_str in dst or "memory/cloud" in dst.replace("\\", "/")
                         or "memory\\cloud" in dst]
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual(before_filemap, after_filemap)
        self.assertEqual(before_catalogs, after_catalogs)
        self.assertEqual([], forbidden)

    def test_failure_paths_never_touch_filemap(self) -> None:
        scenarios: list[str] = []
        for target_state in ("collision", "already_present"):
            with self.subTest(target_state=target_state):
                with tempfile.TemporaryDirectory() as tmp:
                    env = build_promotion_env(tmp, target_state=target_state)
                    filemap_file = _write_real_filemap(env.ws, env.filemap)
                    before = filemap_file.read_bytes()
                    with WriterWriteProbe():
                        self.promote(env)
                    scenarios.append(target_state)
                    self.assertEqual(before, filemap_file.read_bytes())


# ==============================================================================
# End-state discipline
# ==============================================================================

class PromotionEndStateTests(PromotionTestCase):
    def test_all_results_use_only_the_documented_end_states(self) -> None:
        observed: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            observed.append(self.promote(env)["status"])

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp, target_state="already_present")
            observed.append(self.promote(env)["status"])

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp, target_state="collision")
            observed.append(self.promote(env)["status"])

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            tampered = dict(env.preflight)
            tampered["preflight_hash"] = "0" * 64
            observed.append(self.promote(env, preflight=tampered)["status"])

        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            real_unlink = Path.unlink

            def failing_unlink(self, *args, **kwargs):
                if Path(self).resolve() == Path(source).resolve():
                    raise PermissionError("denied")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                observed.append(self.promote(env)["status"])

        for status in observed:
            self.assertIn(status, END_STATES, msg=status)
        self.assertEqual(
            {"promotion_completed", "already_present_verified", "collision_detected",
             "recovery_required", "source_cleanup_pending"},
            set(observed),
        )

    def test_drift_never_reports_promotion_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            tampered = dict(env.preflight)
            tampered["review_hash"] = "0" * 64
            result = self.promote(env, preflight=tampered)
        self.assertNotEqual("promotion_completed", result["status"])


# ==============================================================================
# Fix round 1 / MAJOR: resumed + completed journals must re-verify the real target
# ==============================================================================

class PromotionResumeReverificationTests(PromotionTestCase):
    """A hash-chained journal proves the claim, not the current disk state.

    Before any quarantine-source cleanup and before reporting ``promotion_completed``
    from a completed/resumed journal, the writer must re-open the real target and
    verify size + SHA-256 against the journal-bound source identity.  A missing target
    is ``recovery_required``; a different-hash target is ``collision_detected``; the
    quarantine source is never removed in either case.
    """

    def _source(self, env: PromotionEnv) -> Path:
        return env.ws / Path(env.candidate["source"]["quarantine_path"])

    def _abort_after(self, env: PromotionEnv, phase: str) -> None:
        with self.assertRaises(AbortSimulation):
            self.promote(env, _fault_hook=_hook_after(phase))

    def _promote_with_failing_cleanup(self, env: PromotionEnv) -> dict:
        source = self._source(env)
        real_unlink = Path.unlink

        def failing_unlink(self, *args, **kwargs):
            if Path(self).resolve() == source.resolve():
                raise PermissionError("denied")
            return real_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", failing_unlink):
            return self.promote(env)

    # ------------------------------------------------------------------
    # Abort at target_verified
    # ------------------------------------------------------------------

    def test_retry_after_target_verified_with_deleted_target_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self._abort_after(env, "target_verified")
            target = _target_path(env.ws)
            self.assertTrue(target.is_file())
            target.unlink()
            result = self.promote(env)
            source_kept = self._source(env).exists()
            target_recreated = target.exists()
        self.assertEqual("recovery_required", result["status"])
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertTrue(source_kept, "source must not be deleted when the target is gone")
        self.assertFalse(target_recreated)

    def test_retry_after_target_verified_with_corrupted_target_is_collision(self) -> None:
        corrupted = b"corrupted after abort at target_verified"
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            self._abort_after(env, "target_verified")
            target = _target_path(env.ws)
            target.write_bytes(corrupted)
            result = self.promote(env)
            source_kept = self._source(env).exists()
            target_bytes = target.read_bytes()
        self.assertEqual("collision_detected", result["status"])
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertTrue(source_kept)
        self.assertEqual(corrupted, target_bytes)

    # ------------------------------------------------------------------
    # Source cleanup pending (cleanup already failed once)
    # ------------------------------------------------------------------

    def test_retry_after_source_cleanup_pending_with_deleted_target_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self._promote_with_failing_cleanup(env)
            self.assertEqual("source_cleanup_pending", first["status"])
            target = _target_path(env.ws)
            target.unlink()
            result = self.promote(env)
            source_kept = self._source(env).exists()
            target_recreated = target.exists()
        self.assertEqual("recovery_required", result["status"])
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertTrue(source_kept)
        self.assertFalse(target_recreated)

    def test_retry_after_source_cleanup_pending_with_corrupted_target_is_collision(self) -> None:
        corrupted = b"corrupted after abort at source_cleanup_pending"
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self._promote_with_failing_cleanup(env)
            self.assertEqual("source_cleanup_pending", first["status"])
            target = _target_path(env.ws)
            target.write_bytes(corrupted)
            result = self.promote(env)
            source_kept = self._source(env).exists()
            target_bytes = target.read_bytes()
        self.assertEqual("collision_detected", result["status"])
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertTrue(source_kept)
        self.assertEqual(corrupted, target_bytes)

    # ------------------------------------------------------------------
    # Completed journal
    # ------------------------------------------------------------------

    def test_retry_after_completed_with_deleted_target_is_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self.promote(env)
            self.assertEqual("promotion_completed", first["status"])
            target = _target_path(env.ws)
            target.unlink()
            result = self.promote(env)
            target_recreated = target.exists()
        self.assertEqual("recovery_required", result["status"])
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertFalse(target_recreated)

    def test_retry_after_completed_with_corrupted_target_is_collision(self) -> None:
        corrupted = b"corrupted after completion"
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            first = self.promote(env)
            self.assertEqual("promotion_completed", first["status"])
            target = _target_path(env.ws)
            target.write_bytes(corrupted)
            result = self.promote(env)
            target_bytes = target.read_bytes()
        self.assertEqual("collision_detected", result["status"])
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertEqual(corrupted, target_bytes)


# ==============================================================================
# Fix round 1 / MINOR 2: same-hash reconcile advances the journal
# ==============================================================================

class PromotionRacedSameReconcileTests(PromotionTestCase):
    def test_crash_between_link_and_journal_advances_to_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            real_link = os.link

            def link_then_abort(src, dst, *args, **kwargs):
                real_link(src, dst, *args, **kwargs)
                raise AbortSimulation("crash between link and target_promoted append")

            with self.assertRaises(AbortSimulation):
                with patch("os.link", side_effect=link_then_abort):
                    self.promote(env)
            journal = _read_journal(env.ws)
            self.assertEqual("temp_written", journal["phase"])
            self.assertTrue(_target_path(env.ws).is_file())
            self.assertTrue(source.exists())

            result = self.promote(env)
            journal = _read_journal(env.ws)
            source_gone = not source.exists()
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual("completed", journal["phase"])
        self.assertNotEqual("in_progress", journal["status"])
        self.assertTrue(source_gone)


# ==============================================================================
# Fix round 1 / MINOR 3: mandatory FR-09 test families
# ==============================================================================

class PromotionRequiredTestFamiliesTests(PromotionTestCase):
    def test_cross_volume_source_promotion_is_simulated(self) -> None:
        """A cross-volume source is copied, never hard-linked across volumes.

        The simulated cross-volume primitive raises ``EXDEV`` for any hard link whose
        source is the quarantine file.  The writer reads the bytes and links only its
        own sibling temp (same volume as the target), so the promotion completes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = (env.ws / Path(env.candidate["source"]["quarantine_path"])).resolve()
            target = _target_path(env.ws)
            real_link = os.link

            def exdev_on_source_link(src, dst, *args, **kwargs):
                if Path(src).resolve() == source:
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return real_link(src, dst, *args, **kwargs)

            with patch("os.link", side_effect=exdev_on_source_link):
                result = self.promote(env)
            target_bytes = target.read_bytes()
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual(DEFAULT_DATA, target_bytes)

    def test_writer_level_junction_at_target_parent_fails_closed(self) -> None:
        """A real junction on the target parent is rejected by the writer itself.

        The preflight envelope was built while the parent was a plain directory; the
        junction is swapped in afterwards, so only the writer's own unresolved reparse
        walk can stop it.  Skipped where NTFS junction creation is unavailable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            parent = env.ws / Path(DEFAULT_SCAN_DIR) / Path(DEFAULT_TARGET_DIR)
            internal = parent.parent / "RealCorrespondence"
            internal.mkdir()
            parent.rmdir()
            if not mdp1.create_directory_junction(parent, internal):
                self.skipTest("NTFS directory junction creation unavailable")
            try:
                source = env.ws / Path(env.candidate["source"]["quarantine_path"])
                result = self.promote(env)
                source_kept = source.exists()
                escaped = internal / DEFAULT_FILENAME
                escaped_exists = escaped.exists()
            finally:
                try:
                    parent.rmdir()
                except OSError:
                    pass
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual("unsafe_path", result["reason"])
        self.assertTrue(source_kept)
        self.assertFalse(escaped_exists)

    def test_abort_after_source_cleanup_pending_then_retry_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            real_unlink = Path.unlink

            def failing_unlink(self, *args, **kwargs):
                if Path(self).resolve() == source.resolve():
                    raise PermissionError("denied")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                with self.assertRaises(AbortSimulation):
                    self.promote(env, _fault_hook=_hook_after("source_cleanup_pending"))
            journal = _read_journal(env.ws)
            self.assertEqual("source_cleanup_pending", journal["phase"])
            self.assertTrue(source.exists())
            self.assertTrue(_target_path(env.ws).is_file())

            result = self.promote(env)
            journal = _read_journal(env.ws)
            source_gone = not source.exists()
        self.assertEqual("promotion_completed", result["status"])
        self.assertEqual("completed", journal["phase"])
        self.assertTrue(source_gone)

    def test_retry_from_failed_journal_phase_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            target = _target_path(env.ws)
            real_open = os.open

            def enospc_open(path, flags, *args, **kwargs):
                if _is_target_temp(target.parent, path):
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_open(path, flags, *args, **kwargs)

            with patch("os.open", side_effect=enospc_open):
                first = self.promote(env)
            self.assertEqual("recovery_required", first["status"])
            failed = _read_journal(env.ws)
            self.assertEqual("failed", failed["status"])
            self.assertEqual("temp_written", failed["failed_phase"])

            second = self.promote(env)
            journal = _read_journal(env.ws)
            source_gone = not source.exists()
        self.assertEqual("promotion_completed", second["status"])
        self.assertEqual("completed", journal["phase"])
        self.assertTrue(source_gone)

    def test_flush_error_during_temp_write_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)
            real_open = os.open
            real_fsync = os.fsync
            target_temp_fds: set[int] = set()

            def tracking_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if _is_target_temp(target.parent, path):
                    target_temp_fds.add(fd)
                return fd

            def failing_fsync(fd):
                if fd in target_temp_fds:
                    # One-shot: the failed temp fd is closed and its number can be
                    # reused by the subsequent journal write, which must still fsync.
                    target_temp_fds.discard(fd)
                    raise OSError(errno.EIO, "flush failed")
                return real_fsync(fd)

            with patch("os.open", side_effect=tracking_open):
                with patch("os.fsync", side_effect=failing_fsync):
                    result = self.promote(env)
            source_kept = (env.ws / Path(env.candidate["source"]["quarantine_path"])).exists()
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(target.exists())
        self.assertTrue(source_kept)
        self.assert_no_own_temp(env.ws)

    def test_literal_enospc_never_reports_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            target = _target_path(env.ws)
            real_open = os.open

            def enospc_open(path, flags, *args, **kwargs):
                if _is_target_temp(target.parent, path):
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_open(path, flags, *args, **kwargs)

            with patch("os.open", side_effect=enospc_open):
                result = self.promote(env)
            journal = _read_journal(env.ws)
            source_kept = (env.ws / Path(env.candidate["source"]["quarantine_path"])).exists()
        self.assertNotEqual("promotion_completed", result["status"])
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual("failed", journal["status"])
        self.assertEqual("temp_written", journal["failed_phase"])
        self.assertTrue(source_kept)
        self.assertFalse(target.exists())


# ==============================================================================
# Fix round 1 / MINOR 4: inventory run_dir identical to the MD-P1 verifier
# ==============================================================================

class PromotionDataDirSymmetryTests(PromotionTestCase):
    def test_inventory_cleanup_matches_verifier_under_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            alternate_data_dir = env.ws / "alt-data"
            result = self.promote(env, data_dir=alternate_data_dir)
            target_exists = _target_path(env.ws).is_file()
            inv = json.loads(_inventory_path(env.ws).read_text(encoding="utf-8"))
            removed_from_verified_inventory = (
                normalize_message_id(DEFAULT_MESSAGE_ID) not in inv["messages"]
            )
            stray_inventories = sorted(
                (alternate_data_dir / "attachments").glob("**/.quarantine-inventory.json")
            )
        self.assertEqual("promotion_completed", result["status"])
        self.assertTrue(target_exists)
        self.assertTrue(
            removed_from_verified_inventory,
            "cleanup must rewrite the inventory the MD-P1 verifier pinned",
        )
        self.assertEqual([], stray_inventories)


# ==============================================================================
# Fix round 1 / MINOR 5: stale own journal temp hygiene
# ==============================================================================

class PromotionJournalTempHygieneTests(PromotionTestCase):
    def test_stale_own_journal_temp_is_cleaned_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = build_promotion_env(tmp)
            with self.assertRaises(AbortSimulation):
                self.promote(env, _fault_hook=_hook_after("temp_written"))
            journal_path = _find_journal(env.ws)
            stale = journal_path.parent / f".{JOURNAL_FILENAME}.deadbeef.tmp"
            stale.write_text("orphaned journal temp", encoding="utf-8")
            self.assertTrue(stale.exists())

            result = self.promote(env)
            stale_exists = stale.exists()
        self.assertEqual("promotion_completed", result["status"])
        self.assertFalse(stale_exists, "stale own journal temps must be cleaned on resume")


if __name__ == "__main__":
    unittest.main()
