"""Hermetic TDD tests for FR-09 / MD-P3: Cloud-Atlas refresh handoff + outcome coupling.

These tests are written before the MD-P3 handoff API exists in
``core.attachment_promotion``; the first run fails with an ``ImportError`` for the
missing symbols.  That is the genuine Red gate.

The module describes the MD-P3 mail-desk contract on top of the verified MD-P2
result:

1. ``build_cloud_atlas_refresh_handoff`` derives the ``cloud_atlas_refresh_handoff``
   Schema 1 *only* from a canonically revalidated MD-P2 result
   (``promotion_completed`` / ``already_present_verified``).  The promotion journal
   is revalidated through ``load_promotion_journal``; result / journal / preflight /
   review / candidate hashes and the storage/path/size bindings must all agree.
2. The handoff carries no absolute paths, no mail text and no receiving
   instructions; every text field is bounded structured metadata.
3. ``compose_promotion_outcome`` couples the promotion to the Cloud-Atlas refresh:
   a missing adapter, refresh error, timeout or verify error never changes the
   verified promotion (``promotion_completed_refresh_pending`` with the same
   ``promotion_id``); a verified refresh is ``promotion_completed`` /
   ``refresh_completed``; journal/target drift is ``recovery_required``.  A retry
   only re-runs refresh + verify, never MD-P2.

All filesystem activity happens inside ``tempfile.TemporaryDirectory`` sandboxes and
the whole module performs zero real cloud, mailbox or office access.  Fixture
builders are reused from ``test_maildesk_attachment_promotion_mdp1`` and
``..._mdp2``.
"""

from __future__ import annotations

import json
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
import test_maildesk_attachment_promotion_mdp2 as mdp2  # noqa: E402
from core.attachment_filing import compute_candidate_hash  # noqa: E402
from core.modes.dossier_synthesis import canonical_json_sha256  # noqa: E402
from core import attachment_promotion as promotion  # noqa: E402
from core.attachment_promotion import (  # noqa: E402
    CLOUD_ATLAS_REFRESH_HANDOFF_KIND,
    CLOUD_ATLAS_REFRESH_HANDOFF_OPERATION,
    CLOUD_ATLAS_REFRESH_HANDOFF_RECEIVER,
    PROHIBITED_AUTOMATIC_STEPS,
    PromotionHandoffError,
    REFRESH_RESULT_KIND,
    REFRESH_STATUS_COMPLETED,
    REFRESH_STATUS_DENIED,
    REFRESH_STATUS_PENDING,
    REQUIRED_RECEIVING_STEPS,
    STATUS_PROMOTION_COMPLETED,
    STATUS_PROMOTION_COMPLETED_REFRESH_PENDING,
    STATUS_RECOVERY_REQUIRED,
    build_cloud_atlas_refresh_handoff,
    compose_promotion_outcome,
    load_promotion_journal,
    write_cloud_atlas_refresh_handoff,
)


NOW = mdp1.NOW
DEFAULT_SCAN_DIR = mdp1.DEFAULT_SCAN_DIR
DEFAULT_TARGET_DIR = mdp1.DEFAULT_TARGET_DIR
DEFAULT_FILENAME = mdp1.DEFAULT_FILENAME

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

MAIL_BODY_TOKENS = ("Betreff", "Anhang", "Bitte pruefen", "body", "prompt", "instructions")


def _resign(result: dict) -> dict:
    """Return a copy of a result dict whose ``result_hash`` matches its body."""
    body = {key: value for key, value in result.items() if key != "result_hash"}
    resigned = dict(body)
    resigned["result_hash"] = canonical_json_sha256(body)
    return resigned


def _find_journal(ws: Path) -> Path:
    matches = sorted((ws / "data" / "mail-desk" / "attachment-promotions").glob(
        "*/promotion-journal.json"
    ))
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one journal, found {matches}")
    return matches[0]


def make_refresh_outcome(
    status: str,
    *,
    promotion_id: str,
    target_relative_path: str,
    target_sha256: str,
    reason: str = "test",
    handoff_hash: str = "a" * 64,
    storage_id: str = "primary",
) -> dict:
    body = {
        "schema_version": 1,
        "kind": REFRESH_RESULT_KIND,
        "status": status,
        "reason": reason,
        "promotion_id": promotion_id,
        "handoff_hash": handoff_hash,
        "storage_id": storage_id,
        "scan_dir": DEFAULT_SCAN_DIR,
        "target_relative_path": target_relative_path,
        "target_sha256": target_sha256,
        "filemap_relative_path": None,
        "filemap_sha256": None,
        "storage_ids": [storage_id],
    }
    body["result_hash"] = canonical_json_sha256(body)
    return body


class HandoffTestCase(mdp2.PromotionTestCase):
    """Shared helpers: produce a real completed MD-P2 result and its handoff."""

    def completed_promotion(self, tmp: str, **kwargs) -> tuple:
        env = mdp2.build_promotion_env(tmp, **kwargs)
        result = self.promote(env)
        self.assertEqual(STATUS_PROMOTION_COMPLETED, result["status"])
        return env, result

    def already_present_promotion(self, tmp: str, **kwargs) -> tuple:
        env = mdp2.build_promotion_env(tmp, target_state="already_present", **kwargs)
        result = self.promote(env)
        self.assertEqual("already_present_verified", result["status"])
        return env, result

    def build_handoff(self, env, result) -> dict:
        return build_cloud_atlas_refresh_handoff(
            result, env.candidate, env.filemap, workspace_root=env.ws
        )


# ==============================================================================
# Handoff construction
# ==============================================================================

class HandoffBuilderTests(HandoffTestCase):
    def test_valid_handoff_binds_revalidated_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            handoff = self.build_handoff(env, result)

            self.assertEqual(HANDOFF_KEYS, set(handoff))
            self.assertEqual(1, handoff["schema_version"])
            self.assertEqual(CLOUD_ATLAS_REFRESH_HANDOFF_KIND, handoff["kind"])
            self.assertEqual(CLOUD_ATLAS_REFRESH_HANDOFF_RECEIVER, handoff["receiver"])
            self.assertEqual(CLOUD_ATLAS_REFRESH_HANDOFF_OPERATION, handoff["operation"])
            self.assertEqual(result["promotion_id"], handoff["promotion_id"])
            self.assertEqual(result["journal_hash"], handoff["journal_hash"])
            self.assertEqual(result["candidate_hash"], handoff["candidate_hash"])
            self.assertEqual(result["review_hash"], handoff["review_hash"])
            self.assertEqual(result["preflight_hash"], handoff["preflight_hash"])
            self.assertEqual("project", handoff["scope"])
            self.assertEqual("pilot-proj", handoff["entity_id"])
            self.assertIsNone(handoff["subtopic_id"])
            self.assertEqual("primary", handoff["storage_id"])
            self.assertEqual(DEFAULT_SCAN_DIR, handoff["scan_dir"])
            self.assertEqual(
                f"{DEFAULT_TARGET_DIR}/{DEFAULT_FILENAME}", handoff["target_relative_path"]
            )
            self.assertEqual(result["target_sha256"], handoff["target_sha256"])
            self.assertEqual(result["target_size_bytes"], handoff["target_size_bytes"])
            self.assertRegex(handoff["filemap_snapshot_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(list(REQUIRED_RECEIVING_STEPS), handoff["required_receiving_steps"])
            self.assertEqual(
                list(PROHIBITED_AUTOMATIC_STEPS), handoff["prohibited_automatic_steps"]
            )

    def test_handoff_hash_is_canonical_over_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            handoff = self.build_handoff(env, result)
            body = {key: value for key, value in handoff.items() if key != "handoff_hash"}
            self.assertEqual(canonical_json_sha256(body), handoff["handoff_hash"])

    def test_no_absolute_paths_or_mail_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = mdp2.build_promotion_env(tmp)
            env.candidate["reason"] = "Betreff: Bitte pruefen; body: hidden instructions"
            env.candidate["candidate_hash"] = compute_candidate_hash(env.candidate)
            env.receipt = mdp1.make_receipt(env.candidate, env.filemap)
            env.preflight = promotion.preflight_attachment_promotion(
                env.candidate, env.catalogs, receipt=env.receipt, filemap=env.filemap,
                workspace_root=env.ws, current_time=NOW,
            )
            result = self.promote(env)
            handoff = self.build_handoff(env, result)
            serialized = json.dumps(handoff)
            for token in MAIL_BODY_TOKENS:
                self.assertNotIn(token, serialized)
            for field in ("scan_dir", "target_relative_path", "entity_id", "storage_id"):
                self.assertNotRegex(str(handoff[field]), r"^[A-Za-z]:")
            self.assertNotIn("reason", handoff)
            self.assertNotIn("description", handoff)

    def test_result_hash_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["reason"] = "tampered"
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    tampered, env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_candidate_hash_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["candidate_hash"] = "b" * 64
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_review_hash_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["review_hash"] = "c" * 64
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_preflight_hash_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["preflight_hash"] = "d" * 64
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_target_sha256_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["target_sha256"] = "e" * 64
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_target_size_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["target_size_bytes"] = int(result["target_size_bytes"]) + 1
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_target_path_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["target_relative_path"] = "other/elsewhere.pdf"
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_storage_id_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["storage_id"] = "other"
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_promotion_id_drift_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            tampered = dict(result)
            tampered["promotion_id"] = "f" * 64
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(tampered), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_tampered_candidate_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            env.candidate["destination"]["target_dir"] = "C:/absolute"
            with self.assertRaises(PromotionHandoffError):
                self.build_handoff(env, result)

    def test_tampered_journal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            journal_path = _find_journal(env.ws)
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["source_sha256"] = "9" * 64
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            with self.assertRaises(PromotionHandoffError):
                self.build_handoff(env, result)

    def test_missing_journal_rejected_for_promotion_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            _find_journal(env.ws).unlink()
            with self.assertRaises(PromotionHandoffError):
                self.build_handoff(env, result)

    def test_already_present_result_is_journaled_with_trust_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.already_present_promotion(tmp)
            self.assertIsNotNone(result["journal_relative_path"])
            self.assertIsNotNone(result["journal_hash"])
            journal = json.loads(_find_journal(env.ws).read_text(encoding="utf-8"))
            self.assertEqual(["approved", "preflight_verified", "completed"],
                             [entry["phase"] for entry in journal["phases"]])
            self.assertTrue(journal["already_present"])
            self.assertEqual("completed", journal["status"])
            self.assertEqual(result["journal_hash"], journal["journal_hash"])
            self.assertEqual(result["preflight_hash"], journal["preflight_hash"])

            handoff = self.build_handoff(env, result)
            self.assertEqual(result["journal_hash"], handoff["journal_hash"])
            self.assertEqual(result["journal_relative_path"], handoff["journal_relative_path"])
            self.assertEqual(result["preflight_hash"], handoff["preflight_hash"])

    def test_already_present_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.already_present_promotion(tmp)
            second = self.promote(env)
            self.assertEqual("already_present_verified", second["status"])
            self.assertEqual(result["journal_hash"], second["journal_hash"])

    def test_forged_journal_less_already_present_result_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.already_present_promotion(tmp)
            forged = dict(result)
            forged["journal_relative_path"] = None
            forged["journal_hash"] = None
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(forged), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_already_present_preflight_hash_must_match_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.already_present_promotion(tmp)
            forged = dict(result)
            forged["preflight_hash"] = "a" * 64
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(forged), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_promotion_completed_null_journal_fields_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            forged = dict(result)
            forged["journal_relative_path"] = None
            forged["journal_hash"] = None
            with self.assertRaises(PromotionHandoffError):
                build_cloud_atlas_refresh_handoff(
                    _resign(forged), env.candidate, env.filemap, workspace_root=env.ws
                )

    def test_incomplete_promotion_status_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            for status in ("collision_detected", STATUS_RECOVERY_REQUIRED):
                with self.subTest(status=status):
                    tampered = dict(result)
                    tampered["status"] = status
                    with self.assertRaises(PromotionHandoffError):
                        build_cloud_atlas_refresh_handoff(
                            _resign(tampered), env.candidate, env.filemap,
                            workspace_root=env.ws,
                        )

    def test_malformed_inputs_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            for bad in (None, [], "nope", {}):
                with self.subTest(bad=bad):
                    with self.assertRaises(PromotionHandoffError):
                        build_cloud_atlas_refresh_handoff(
                            bad, env.candidate, env.filemap, workspace_root=env.ws
                        )

    def test_write_handoff_atomic_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self.completed_promotion(tmp)
            handoff = self.build_handoff(env, result)
            out = env.ws / "data" / "mail-desk" / "cloud-atlas-refresh-handoff.json"
            written = write_cloud_atlas_refresh_handoff(handoff, out)
            self.assertEqual(out, Path(written))
            self.assertEqual(handoff, json.loads(out.read_text(encoding="utf-8")))
            self.assertEqual([], [p.name for p in out.parent.glob("*.tmp")])


# ==============================================================================
# Fix round 2 / MAJOR: retry after a terminal failed@temp_written journal
# ==============================================================================

class FailedJournalRetryTests(HandoffTestCase):
    """A terminal ``failed`` journal forbids the already-present shortcut.

    The writer must retry the failed phase first; appending ``completed`` directly onto
    a ``failed`` entry produces a journal that every later ``load_promotion_journal``
    rejects as contradictory.
    """

    def _fail_at_temp_written(self, env, source) -> dict:
        """Drive one promotion run that records failed@temp_written via real source drift."""
        real_preflight = promotion.preflight_attachment_promotion

        def drifting_preflight(*args, **kwargs):
            envelope = real_preflight(*args, **kwargs)
            if envelope.get("status") in ("ready", "already_present"):
                source.unlink()  # the source drifts away after the preflight
            return envelope

        with patch.object(promotion, "preflight_attachment_promotion",
                          side_effect=drifting_preflight):
            return self.promote(env)

    def test_already_present_retry_after_failed_temp_written_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = mdp2.build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            target = env.ws / Path(DEFAULT_SCAN_DIR) / Path(DEFAULT_TARGET_DIR) / DEFAULT_FILENAME

            first = self._fail_at_temp_written(env, source)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, first["status"])
            failed = json.loads(mdp2._find_journal(env.ws).read_text(encoding="utf-8"))
            self.assertEqual("failed", failed["status"])
            self.assertEqual("temp_written", failed["failed_phase"])
            self.assertFalse(source.exists())
            self.assertFalse(target.exists())

            # Restore the source and place the identical external target: the retry must
            # reconcile the failed phase forward instead of shortcutting.
            source.write_bytes(mdp1.DEFAULT_DATA)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(mdp1.DEFAULT_DATA)

            second = self.promote(env)
            self.assertIn(second["status"], (STATUS_PROMOTION_COMPLETED,
                                             "already_present_verified"))
            journal = load_promotion_journal(mdp2._find_journal(env.ws))
            self.assertEqual("completed", journal["status"])
            phases = [entry["phase"] for entry in journal["phases"]]
            failed_index = phases.index("failed")
            self.assertEqual("temp_written", phases[failed_index + 1],
                             "the failed phase must be retried before completion")
            self.assertNotIn("completed", phases[:failed_index])
            source_gone = not source.exists()
            handoff = self.build_handoff(env, second)
        self.assertTrue(source_gone, "the reconciled retry cleans the quarantine source")
        self.assertEqual(second["journal_hash"], handoff["journal_hash"])
        self.assertEqual(second["target_sha256"], handoff["target_sha256"])

    def test_unretryable_failed_temp_written_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = mdp2.build_promotion_env(tmp)
            source = env.ws / Path(env.candidate["source"]["quarantine_path"])
            target = env.ws / Path(DEFAULT_SCAN_DIR) / Path(DEFAULT_TARGET_DIR) / DEFAULT_FILENAME

            first = self._fail_at_temp_written(env, source)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, first["status"])
            self.assertFalse(source.exists())

            # The external target appears with identical bytes, but the source stays
            # missing: the failed phase cannot be retried, so the writer must fail closed
            # instead of taking the already-present shortcut.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(mdp1.DEFAULT_DATA)
            source.write_bytes(mdp1.DEFAULT_DATA)
            real_preflight = promotion.preflight_attachment_promotion

            def drifting_preflight(*args, **kwargs):
                envelope = real_preflight(*args, **kwargs)
                if envelope.get("status") in ("ready", "already_present"):
                    source.unlink()  # drifts again before the retry write
                return envelope

            with patch.object(promotion, "preflight_attachment_promotion",
                              side_effect=drifting_preflight):
                second = self.promote(env)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, second["status"])
            journal = load_promotion_journal(mdp2._find_journal(env.ws))
            self.assertEqual("failed", journal["status"])
            self.assertEqual("temp_written", journal["failed_phase"])
            completed = [entry for entry in journal["phases"] if entry["phase"] == "completed"]
        self.assertEqual([], completed, "completed must never be appended onto a failed entry")


# ==============================================================================
# Fix round 2 / MINOR: subtopic-owned storages reach the consumer
# ==============================================================================

class SubtopicHandoffTests(HandoffTestCase):
    """The handoff must carry the decision-bound subtopic id for subtopic storages."""

    TOPIC_ID = "topic-a"
    SUBTOPIC_ID = "sub-1"
    SUB_SCAN_DIR = "data/cloud/TOPIC-A/sub"
    SUB_TARGET_DIR = "01_Admin"
    SUB_OUTPUT_DIR = "memory/cloud/topics/topic-a/sub-1"

    def _build_subtopic_env(self, tmp: str) -> mdp2.PromotionEnv:
        storage_cfg = {
            "scan_dir": self.SUB_SCAN_DIR,
            "target_dir": self.SUB_TARGET_DIR,
            "output_json": f"{self.SUB_OUTPUT_DIR}/filemap.json",
            "output_md": f"{self.SUB_OUTPUT_DIR}/filemap.md",
            "output_dir": self.SUB_OUTPUT_DIR,
        }
        catalogs = {
            "projects": [],
            "topics": [{
                "id": self.TOPIC_ID,
                "title": "Topic A",
                "subtopics": [{
                    "id": self.SUBTOPIC_ID,
                    "title": "Sub One",
                    "status": "active",
                    "cloud_sync": {"primary": storage_cfg},
                }],
            }],
        }
        filemap = mdp1.make_filemap(
            scope="topic", storage_id="primary", project=self.TOPIC_ID,
            scan_dir=self.SUB_SCAN_DIR, output_dir=self.SUB_OUTPUT_DIR, files={},
        )
        ws = Path(tmp)
        mdp1.write_quarantine(ws)
        (ws / Path(self.SUB_SCAN_DIR) / Path(self.SUB_TARGET_DIR)).mkdir(
            parents=True, exist_ok=True
        )
        candidate = mdp1.make_candidate(
            filemap=filemap, scan_dir=self.SUB_SCAN_DIR,
            target_dir=self.SUB_TARGET_DIR, storage_id="primary",
        )
        receipt = mdp1.make_receipt(candidate, filemap)
        decision = {"kind": "topic", "id": self.TOPIC_ID, "subtopic": self.SUBTOPIC_ID}
        preflight = promotion.preflight_attachment_promotion(
            candidate, catalogs, receipt=receipt, filemap=filemap,
            workspace_root=ws, current_time=NOW, decision=decision,
        )
        return mdp2.PromotionEnv(
            ws=ws, filemap=filemap, catalogs=catalogs, candidate=candidate,
            receipt=receipt, preflight=preflight, decision=decision,
        )

    def test_subtopic_handoff_carries_bound_subtopic_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._build_subtopic_env(tmp)
            self.assertEqual("ready", env.preflight["status"])
            result = self.promote(env, decision=env.decision)
            self.assertEqual(STATUS_PROMOTION_COMPLETED, result["status"])
            handoff = build_cloud_atlas_refresh_handoff(
                result, env.candidate, env.filemap, workspace_root=env.ws
            )
        self.assertEqual("topic", handoff["scope"])
        self.assertEqual(self.TOPIC_ID, handoff["entity_id"])
        self.assertEqual(self.SUBTOPIC_ID, handoff["subtopic_id"])
        self.assertEqual("primary", handoff["storage_id"])


# ==============================================================================
# Outcome coupling
# ==============================================================================

class OutcomeCouplingTests(HandoffTestCase):
    def _completed(self, tmp: str):
        env, result = self.completed_promotion(tmp)
        return env, result

    def test_missing_adapter_keeps_verified_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            combined = compose_promotion_outcome(result, None)
            self.assertEqual(STATUS_PROMOTION_COMPLETED_REFRESH_PENDING, combined["status"])
            self.assertEqual(result["promotion_id"], combined["promotion_id"])
            self.assertEqual(result["target_sha256"], combined["target_sha256"])

    def test_refresh_pending_couples_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            outcome = make_refresh_outcome(
                REFRESH_STATUS_PENDING, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="converter_error",
            )
            combined = compose_promotion_outcome(result, outcome)
            self.assertEqual(STATUS_PROMOTION_COMPLETED_REFRESH_PENDING, combined["status"])
            self.assertEqual(REFRESH_STATUS_PENDING, combined["refresh_status"])
            self.assertEqual(result["promotion_id"], combined["promotion_id"])

    def test_refresh_completed_couples_to_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            outcome = make_refresh_outcome(
                REFRESH_STATUS_COMPLETED, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="refresh_completed",
            )
            combined = compose_promotion_outcome(result, outcome)
            self.assertEqual(STATUS_PROMOTION_COMPLETED, combined["status"])
            self.assertEqual(REFRESH_STATUS_COMPLETED, combined["refresh_status"])
            self.assertEqual(result["promotion_id"], combined["promotion_id"])

    def test_journal_drift_yields_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            outcome = make_refresh_outcome(
                REFRESH_STATUS_DENIED, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="journal_drift",
            )
            combined = compose_promotion_outcome(result, outcome)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, combined["status"])

    def test_target_drift_yields_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            outcome = make_refresh_outcome(
                REFRESH_STATUS_DENIED, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="target_drift",
            )
            combined = compose_promotion_outcome(result, outcome)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, combined["status"])

    def test_mismatched_refresh_target_yields_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            outcome = make_refresh_outcome(
                REFRESH_STATUS_COMPLETED, promotion_id=result["promotion_id"],
                target_relative_path="other/elsewhere.pdf",
                target_sha256=result["target_sha256"], reason="refresh_completed",
            )
            combined = compose_promotion_outcome(result, outcome)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, combined["status"])

    def test_tampered_refresh_outcome_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            outcome = make_refresh_outcome(
                REFRESH_STATUS_COMPLETED, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="refresh_completed",
            )
            outcome["reason"] = "tampered"
            combined = compose_promotion_outcome(result, outcome)
            self.assertEqual(STATUS_PROMOTION_COMPLETED_REFRESH_PENDING, combined["status"])

    def test_promotion_recovery_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            tampered = dict(result)
            tampered["status"] = STATUS_RECOVERY_REQUIRED
            combined = compose_promotion_outcome(_resign(tampered), None)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, combined["status"])

    def test_compose_revalidates_journal_anchor(self) -> None:
        # Fix round 2 cleanup: compose revalidates the promotion journal, closing the
        # asymmetry with build_cloud_atlas_refresh_handoff.
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            journal_path = _find_journal(env.ws)
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["source_sha256"] = "9" * 64
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            combined = compose_promotion_outcome(result, None, workspace_root=env.ws)
            self.assertEqual(STATUS_RECOVERY_REQUIRED, combined["status"])

    def test_retry_never_runs_a_second_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, result = self._completed(tmp)
            pending = make_refresh_outcome(
                REFRESH_STATUS_PENDING, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="converter_error",
            )
            completed = make_refresh_outcome(
                REFRESH_STATUS_COMPLETED, promotion_id=result["promotion_id"],
                target_relative_path=result["target_relative_path"],
                target_sha256=result["target_sha256"], reason="refresh_completed",
            )
            with patch.object(promotion, "promote_attachment",
                              side_effect=AssertionError("MD-P2 must not re-run")) as promo:
                first = compose_promotion_outcome(result, pending)
                second = compose_promotion_outcome(result, completed)
            self.assertEqual(STATUS_PROMOTION_COMPLETED_REFRESH_PENDING, first["status"])
            self.assertEqual(STATUS_PROMOTION_COMPLETED, second["status"])
            self.assertEqual(0, promo.call_count)
            self.assertEqual(result["promotion_id"], second["promotion_id"])


if __name__ == "__main__":
    unittest.main()
