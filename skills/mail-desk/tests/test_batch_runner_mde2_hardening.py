"""FR-15 / MD-E2-T02 hardening tests at the public ``run_draft_mode`` seam.

These tests fix the T02 fail-closed boundary: every bounded MD-E1 outcome stays item-local
and Review/``INBOX``, a ready handoff is revalidated with the canonical validator before any
classification, continued ambiguity preserves the MD-E1 authorization and safe ``files[]``,
the classifier revision binds code-level rules and the consumed hashes, and a repeated
default ``draft`` invocation reuses the canonical MD-E1 ``already_fetched`` path.

The production seams under test are ``run_draft_mode(config, account, data_dir,
dependencies)`` and the real ``attachment_evaluate`` backend; no private helper is asserted
unless it is the only honest way to observe an external boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_batch_runner as runner  # noqa: E402
from core import attachments  # noqa: E402
from core import attachment_evaluation as aevaluate  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
from core import attachment_reclassification as reclass  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
import mde2_fixtures as fixtures  # noqa: E402

_ATTACHMENT_TEXT = fixtures.DEFAULT_TEXT
_ATTACHMENT_SHA = hashlib.sha256(_ATTACHMENT_TEXT.encode("utf-8")).hexdigest()
_ABS_PATH_RE = r"[A-Za-z]:\\\\"


def _ambiguous_decision() -> dict[str, object]:
    return {
        "kind": "unknown",
        "id": "unclassified",
        "confidence": "low",
        "needs_reply": False,
        "review_required": True,
        "review_reason": "insufficient_context",
    }


def _clear_decision() -> dict[str, object]:
    return {"kind": "project", "id": "pilot", "confidence": "high", "needs_reply": False}


def _clear_replacement() -> dict[str, object]:
    return {
        "decision": _clear_decision(),
        "action": {"type": "copy_as_move", "target_folder": "Projekte/Pilot"},
        "notes": "reclassified by attachment",
        "evidence": {"file": "memory/references/projects/pilot/evidence/2026-09.md"},
        "synthesis_targets": [],
    }


def _email(envelope_id: str = "2", message_id: str = "ambiguous@example.test") -> dict[str, object]:
    return {"envelope_id": envelope_id, "folder": "INBOX", "message_id": message_id}


def _item(
    *,
    envelope_id: str = "2",
    message_id: str = "ambiguous@example.test",
    action: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "envelope_id": envelope_id,
        "source_folder": "INBOX",
        "message_id": message_id,
        "decision": _ambiguous_decision(),
        "action": action or {"type": "keep_in_folder", "target_folder": "INBOX"},
    }


def _ready(
    *,
    envelope_id: str = "2",
    message_id: str = "ambiguous@example.test",
    decision: dict[str, object] | None = None,
    text: str = _ATTACHMENT_TEXT,
) -> dict[str, object]:
    return fixtures.canonical_ready_evaluation(
        account="primary",
        folder="INBOX",
        envelope_id=envelope_id,
        message_id=message_id,
        decision=_ambiguous_decision() if decision is None else decision,
        text=text,
    )


def _envelope(
    status: str,
    reason: str,
    authorization: str,
    *,
    files: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "attachment_evaluation": {
            "status": status,
            "reason": reason,
            "authorization": authorization,
            "files": list(files or []),
            "used_for_classification": False,
            "classifier_revision": None,
        }
    }


class _Tracker:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def step(self, *_args: object, **_kwargs: object) -> None:
        pass

    def advance_item(self, *_args: object, **_kwargs: object) -> None:
        pass

    def complete(self, *_args: object, **_kwargs: object) -> None:
        pass


def _draft_with_sources(items: list[dict[str, object]]):
    def _draft(emails_arg, *, source_sink=None, **_kwargs):
        if source_sink is not None:
            for email in emails_arg:
                source_sink(email)
        return {"items": items}

    return _draft


def _run_draft(
    temporary: str,
    items: list[dict[str, object]],
    emails: list[dict[str, object]],
    *,
    config: dict[str, object] | None = None,
    evaluate: Mock,
    reclassify: Mock,
    raw_reader: Mock,
) -> tuple[dict[str, object], Path, str]:
    data_dir = Path(temporary) / "data" / "mail-desk"
    data_dir.mkdir(parents=True)
    output_path = data_dir / "batch-manifest.json"
    merged: dict[str, object] = {"count": len(emails), "output_file": str(output_path)}
    merged.update(config or {})
    result = draft_mode.run_draft_mode(
        merged,
        account="primary",
        data_dir=data_dir,
        dependencies={
            "BatchProgressTracker": _Tracker,
            "atomic_write_json": runner.atomic_write_json,
            "draft_manifest": _draft_with_sources(items),
            "get_unprocessed_emails": Mock(return_value=(emails, 0)),
            "load_sent_index": Mock(return_value={}),
            "attachment_evaluate": evaluate,
            "classify_email": reclassify,
            "fetch_raw_message_eml": raw_reader,
        },
    )
    return result, output_path, output_path.read_text(encoding="utf-8")


class Mde2BoundedOutcomeTests(unittest.TestCase):
    """Every bounded MD-E1 failure/no-op stays item-local and Review/INBOX at false/null."""

    _FAILURES = (
        ("lock_unavailable", "not_applicable"),
        ("policy_blocked", "not_applicable"),
        ("quota_exceeded", "not_applicable"),
        ("fetch_failed", "not_applicable"),
        ("extraction_failed", "not_applicable"),
        ("handoff_invalid", "not_applicable"),
    )

    def test_bounded_failures_are_item_local_and_forced_to_review_inbox(self) -> None:
        for reason, authorization in self._FAILURES:
            with self.subTest(reason=reason):
                item = _item(action={"type": "copy_as_move", "target_folder": "Projekte/Else"})
                evaluate = Mock(return_value=_envelope("failed", reason, authorization))
                reclassify = Mock()
                with tempfile.TemporaryDirectory() as temporary:
                    _result, _output, persisted = _run_draft(
                        temporary,
                        [item],
                        [_email()],
                        evaluate=evaluate,
                        reclassify=reclassify,
                        raw_reader=Mock(return_value=b"raw"),
                    )
                reclassify.assert_not_called()
                installed = item["attachment_evaluation"]
                self.assertEqual(
                    {"status", "reason", "authorization", "files",
                     "used_for_classification", "classifier_revision"},
                    set(installed),
                )
                self.assertEqual("failed", installed["status"])
                self.assertEqual(reason, installed["reason"])
                self.assertEqual("not_applicable", installed["authorization"])
                self.assertEqual([], installed["files"])
                self.assertIs(False, installed["used_for_classification"])
                self.assertIsNone(installed["classifier_revision"])
                self.assertEqual({"type": "keep_in_folder", "target_folder": "INBOX"}, item["action"])
                self.assertIs(True, item["decision"]["review_required"])
                self.assertEqual("low", item["decision"]["confidence"])
                self.assertNotIn("prompt_content", persisted)
                self.assertNotRegex(persisted, _ABS_PATH_RE)

    def test_bounded_no_ops_pass_through_without_reclassification(self) -> None:
        for reason in ("classification_clear", "no_attachments", "no_allowed_attachments"):
            with self.subTest(reason=reason):
                item = _item()
                evaluate = Mock(return_value=_envelope("not_needed", reason, "not_applicable"))
                reclassify = Mock()
                with tempfile.TemporaryDirectory() as temporary:
                    _run_draft(
                        temporary,
                        [item],
                        [_email()],
                        evaluate=evaluate,
                        reclassify=reclassify,
                        raw_reader=Mock(return_value=b"raw"),
                    )
                reclassify.assert_not_called()
                self.assertEqual("not_needed", item["attachment_evaluation"]["status"])
                self.assertEqual(reason, item["attachment_evaluation"]["reason"])
                self.assertIs(False, item["attachment_evaluation"]["used_for_classification"])
                self.assertIsNone(item["attachment_evaluation"]["classifier_revision"])

    def test_md_e1_still_ambiguous_preserves_authorization_and_safe_files(self) -> None:
        safe_file = {
            "filename": "clue.txt", "sha256": "a" * 64, "mime_type": "text/plain",
            "chars": 31, "coverage": "truncated", "run_id": "run-x",
        }
        item = _item(action={"type": "copy_as_move", "target_folder": "Projekte/Else"})
        evaluate = Mock(return_value=_envelope(
            "completed", "still_ambiguous", "auto_evaluated", files=[safe_file]
        ))
        reclassify = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary, [item], [_email()],
                evaluate=evaluate, reclassify=reclassify, raw_reader=Mock(return_value=b"raw"),
            )
        reclassify.assert_not_called()
        installed = item["attachment_evaluation"]
        self.assertEqual("completed", installed["status"])
        self.assertEqual("still_ambiguous", installed["reason"])
        self.assertEqual("auto_evaluated", installed["authorization"])
        self.assertEqual([safe_file], installed["files"])
        self.assertIs(False, installed["used_for_classification"])
        self.assertIsNone(installed["classifier_revision"])
        self.assertEqual({"type": "keep_in_folder", "target_folder": "INBOX"}, item["action"])

    def test_identity_pairing_failures_are_binding_failures_not_no_ops(self) -> None:
        item = _item()
        evaluate = Mock()
        reclassify = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary, [item], [_email(envelope_id="999")],
                evaluate=evaluate, reclassify=reclassify, raw_reader=Mock(),
            )
        evaluate.assert_not_called()
        reclassify.assert_not_called()
        self.assertEqual("failed", item["attachment_evaluation"]["status"])
        self.assertEqual("handoff_invalid", item["attachment_evaluation"]["reason"])
        self.assertIs(False, item["attachment_evaluation"]["used_for_classification"])

    def test_duplicate_source_identity_is_a_binding_failure(self) -> None:
        item = _item()
        evaluate = Mock()
        reclassify = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary,
                [item],
                [_email(), _email()],
                evaluate=evaluate, reclassify=reclassify, raw_reader=Mock(),
            )
        evaluate.assert_not_called()
        self.assertEqual("handoff_invalid", item["attachment_evaluation"]["reason"])


class Mde2ContractViolationTests(unittest.TestCase):
    """Unexpected backend/programmer contract errors fail loud, never relabelled."""

    def _run_expecting_contract_error(self, evaluate: Mock, reclassify: Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(reclass.AttachmentReclassificationContractError):
                _run_draft(
                    temporary,
                    [_item()],
                    [_email()],
                    evaluate=evaluate,
                    reclassify=reclassify,
                    raw_reader=Mock(return_value=b"raw"),
                )

    def test_non_mapping_backend_result_fails_loud(self) -> None:
        self._run_expecting_contract_error(Mock(return_value="not-a-mapping"), Mock())

    def test_non_mapping_staged_object_fails_loud(self) -> None:
        self._run_expecting_contract_error(Mock(return_value={"attachment_evaluation": "x"}), Mock())

    def test_unexpected_status_reason_or_authorization_fail_loud(self) -> None:
        cases = [
            _envelope("weird", "handoff_ready", "auto_evaluated"),
            _envelope("completed", "weird", "auto_evaluated"),
            _envelope("completed", "handoff_ready", "weird"),
        ]
        for case in cases:
            with self.subTest(case=case):
                self._run_expecting_contract_error(Mock(return_value=case), Mock())

    def test_malformed_files_vocabulary_fails_loud(self) -> None:
        cases = [
            {"attachment_evaluation": {
                "status": "completed", "reason": "handoff_ready", "authorization": "auto_evaluated",
                "files": "x", "used_for_classification": False, "classifier_revision": None}},
            {"attachment_evaluation": {
                "status": "completed", "reason": "handoff_ready", "authorization": "auto_evaluated",
                "files": ["x"], "used_for_classification": False, "classifier_revision": None}},
            {"attachment_evaluation": {
                "status": "completed", "reason": "handoff_ready", "authorization": "auto_evaluated",
                "files": [{"sha256": "zz", "coverage": "full"}],
                "used_for_classification": False, "classifier_revision": None}},
            {"attachment_evaluation": {
                "status": "completed", "reason": "handoff_ready", "authorization": "auto_evaluated",
                "files": [{"sha256": _ATTACHMENT_SHA, "coverage": "weird"}],
                "used_for_classification": False, "classifier_revision": None}},
        ]
        for case in cases:
            with self.subTest(case=case):
                self._run_expecting_contract_error(Mock(return_value=case), Mock())

    def test_non_mapping_reclassifier_result_fails_loud(self) -> None:
        self._run_expecting_contract_error(Mock(return_value=_ready()), Mock(return_value="x"))

    def test_malformed_reclassifier_decision_fails_loud(self) -> None:
        self._run_expecting_contract_error(
            Mock(return_value=_ready()), Mock(return_value={"decision": "x"})
        )


class Mde2ReadyEnvelopeContractTests(unittest.TestCase):
    """Fix round 1: an impossible/malformed ready envelope must fail loud, not install.

    A staged ``completed/handoff_ready/auto_evaluated`` result is only meaningful together
    with one validated, ``ready`` handoff sibling whose status agrees with the staged
    reason.  A missing/non-mapping/non-ready handoff, or a staged terminal reason that
    contradicts a supplied ready handoff, is a backend contract violation: it must raise
    ``AttachmentReclassificationContractError`` through the mode seam, write no final
    evaluation, and never call the reclassifier.
    """

    def _staged_ready_and_handoff(self) -> tuple[dict[str, object], dict[str, object]]:
        ready = _ready()
        return ready["attachment_evaluation"], ready["attachment_analysis_handoff"]

    def _assert_contract_error(
        self, envelope: dict[str, object], forbidden_tokens: list[str]
    ) -> None:
        item = _item()
        evaluate = Mock(return_value=envelope)
        reclassify = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "data" / "mail-desk" / "batch-manifest.json"
            with self.assertRaises(reclass.AttachmentReclassificationContractError) as ctx:
                _run_draft(
                    temporary, [item], [_email()],
                    evaluate=evaluate, reclassify=reclassify, raw_reader=Mock(return_value=b"raw"),
                )
            self.assertFalse(
                manifest_path.exists(),
                "no final evaluation/manifest may be written for a contract violation",
            )
        reclassify.assert_not_called()
        message = str(ctx.exception)
        self.assertTrue(message.strip(), "the contract error must carry a bounded message")
        self.assertNotRegex(message, _ABS_PATH_RE)
        for token in forbidden_tokens:
            self.assertNotIn(token, message)

    def test_missing_handoff_sibling_fails_loud(self) -> None:
        staged, _handoff = self._staged_ready_and_handoff()
        self._assert_contract_error(
            {"attachment_evaluation": staged},
            ["prompt_content", "untrusted_attachment_content"],
        )

    def test_non_mapping_handoff_sibling_fails_loud(self) -> None:
        staged, _handoff = self._staged_ready_and_handoff()
        self._assert_contract_error(
            {"attachment_evaluation": staged, "attachment_analysis_handoff": "not-a-mapping"},
            ["prompt_content", "untrusted_attachment_content"],
        )

    def test_non_ready_handoff_status_fails_loud(self) -> None:
        staged, handoff = self._staged_ready_and_handoff()
        contradictory = copy.deepcopy(handoff)
        contradictory["status"] = "blocked_on_required_attachment"
        self._assert_contract_error(
            {"attachment_evaluation": staged, "attachment_analysis_handoff": contradictory},
            ["prompt_content", "untrusted_attachment_content", _ATTACHMENT_TEXT],
        )

    def test_contradictory_staged_reason_with_ready_handoff_fails_loud(self) -> None:
        staged, handoff = self._staged_ready_and_handoff()
        contradictory_staged = {
            "status": "completed",
            "reason": "still_ambiguous",
            "authorization": "auto_evaluated",
            "files": list(staged["files"]),
            "used_for_classification": False,
            "classifier_revision": None,
        }
        self._assert_contract_error(
            {"attachment_evaluation": contradictory_staged, "attachment_analysis_handoff": handoff},
            ["prompt_content", "untrusted_attachment_content", _ATTACHMENT_TEXT],
        )


class Mde2HandoffRevalidationTests(unittest.TestCase):
    """A manipulated/missing/duplicate/hash-mismatched ready handoff stops fail-closed."""

    def _assert_handoff_invalid(self, evaluate: Mock) -> None:
        item = _item()
        reclassify = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary, [item], [_email()],
                evaluate=evaluate, reclassify=reclassify, raw_reader=Mock(return_value=b"raw"),
            )
        reclassify.assert_not_called()
        self.assertEqual("failed", item["attachment_evaluation"]["status"])
        self.assertEqual("handoff_invalid", item["attachment_evaluation"]["reason"])
        self.assertEqual([], item["attachment_evaluation"]["files"])
        self.assertIs(False, item["attachment_evaluation"]["used_for_classification"])
        self.assertIsNone(item["attachment_evaluation"]["classifier_revision"])

    def test_missing_canonical_inventory_is_rejected(self) -> None:
        ready = _ready()
        ready["attachment_analysis_handoff"].pop("canonical_parts", None)
        self._assert_handoff_invalid(Mock(return_value=ready))

    def test_item_hash_drift_against_canonical_inventory_is_rejected(self) -> None:
        ready = _ready()
        ready["attachment_analysis_handoff"]["items"][0]["source_sha256"] = "b" * 64
        self._assert_handoff_invalid(Mock(return_value=ready))

    def test_duplicate_embedded_canonical_part_is_rejected(self) -> None:
        ready = _ready()
        parts = ready["attachment_analysis_handoff"]["canonical_parts"]
        parts.append(dict(parts[0]))
        self._assert_handoff_invalid(Mock(return_value=ready))

    def test_tampered_prompt_content_is_rejected(self) -> None:
        ready = _ready()
        ready["attachment_analysis_handoff"]["prompt_content"] = "tampered"
        self._assert_handoff_invalid(Mock(return_value=ready))

    def test_staged_hash_mismatch_against_handoff_is_rejected(self) -> None:
        ready = _ready()
        ready["attachment_evaluation"]["files"][0]["sha256"] = "c" * 64
        self._assert_handoff_invalid(Mock(return_value=ready))

    def test_missing_staged_hashes_are_rejected(self) -> None:
        ready = _ready()
        ready["attachment_evaluation"]["files"] = []
        self._assert_handoff_invalid(Mock(return_value=ready))

    def test_duplicate_consumed_hashes_are_rejected(self) -> None:
        ready = fixtures.canonical_ready_evaluation_multi(
            account="primary", folder="INBOX", envelope_id="2",
            message_id="ambiguous@example.test", decision=_ambiguous_decision(),
            parts=[("2", "one.txt", _ATTACHMENT_TEXT, "text/plain"),
                   ("3", "two.txt", _ATTACHMENT_TEXT, "text/plain")],
        )
        self._assert_handoff_invalid(Mock(return_value=ready))


class Mde2StillAmbiguousTests(unittest.TestCase):
    """After exactly one reclassification, continued ambiguity preserves MD-E1 state."""

    def test_still_ambiguous_preserves_files_and_adopts_nothing(self) -> None:
        ready = _ready()
        item = _item()
        reclassify = Mock(return_value={
            "decision": _ambiguous_decision(),
            "action": {"type": "copy_as_move", "target_folder": "Projekte/Invented"},
        })
        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary, [item], [_email()],
                evaluate=Mock(return_value=ready), reclassify=reclassify,
                raw_reader=Mock(return_value=b"raw"),
            )
        reclassify.assert_called_once()
        installed = item["attachment_evaluation"]
        self.assertEqual("completed", installed["status"])
        self.assertEqual("still_ambiguous", installed["reason"])
        self.assertEqual("auto_evaluated", installed["authorization"])
        self.assertEqual(ready["attachment_evaluation"]["files"], installed["files"])
        self.assertIs(False, installed["used_for_classification"])
        self.assertIsNone(installed["classifier_revision"])
        # No replacement is adopted and the item stays Review/INBOX.
        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual({"type": "keep_in_folder", "target_folder": "INBOX"}, item["action"])

    def test_truncation_coverage_is_visible_and_preserved(self) -> None:
        long_text = "PILOT-SIGNAL " * 3000
        ready = _ready(text=long_text)
        item = _item()
        captured: dict[str, object] = {}

        def reclassify(source, **kwargs):
            captured["untrusted"] = kwargs.get("untrusted_external_text")
            return {"decision": _ambiguous_decision(), "action": {}}

        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary, [item], [_email()],
                evaluate=Mock(return_value=ready), reclassify=reclassify,
                raw_reader=Mock(return_value=b"raw"),
            )
        installed = item["attachment_evaluation"]
        self.assertEqual("truncated", installed["files"][0]["coverage"])
        self.assertLessEqual(installed["files"][0]["chars"], 15_000)
        self.assertIn("[... Truncated", str(captured["untrusted"]))


class Mde2RevisionTests(unittest.TestCase):
    """The revision deterministically binds the code-level rules and consumed hashes."""

    def test_revision_is_order_insensitive_and_content_sensitive(self) -> None:
        fingerprint = reclass.classifier_rules_fingerprint(MAIL_DESK_ROOT)
        self.assertEqual(
            reclass.compute_classifier_revision(fingerprint, ["b" * 64, "a" * 64]),
            reclass.compute_classifier_revision(fingerprint, ["a" * 64, "b" * 64]),
        )
        self.assertNotEqual(
            reclass.compute_classifier_revision(fingerprint, ["a" * 64]),
            reclass.compute_classifier_revision(fingerprint, ["b" * 64]),
        )
        self.assertNotEqual(
            reclass.compute_classifier_revision(fingerprint, ["a" * 64]),
            reclass.compute_classifier_revision("other-rules", ["a" * 64]),
        )

    def test_catalog_content_change_moves_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            missing = reclass.classifier_rules_fingerprint(workspace)
            catalog = workspace / "memory" / "references" / "projects" / "projects.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text('{"projects": []}', encoding="utf-8")
            empty = reclass.classifier_rules_fingerprint(workspace)
            catalog.write_text('{"projects": [{"id": "pilot"}]}', encoding="utf-8")
            populated = reclass.classifier_rules_fingerprint(workspace)
        self.assertEqual(3, len({missing, empty, populated}))

    def test_code_rule_change_moves_fingerprint_but_comments_and_paths_do_not(self) -> None:
        # MD-M1: the fingerprint binds the ordered active rule source set; the facade
        # ``classifier.py`` entry carries the code-level rules.
        sources = tuple(reclass._CLASSIFIER_MODULE_PATHS)
        facade_index = next(
            index for index, path in enumerate(sources) if Path(path).name == "classifier.py"
        )
        source = Path(sources[facade_index]).read_text(encoding="utf-8")
        self.assertIn('"Junk"', source)

        def _with_facade_replaced(replacement: Path) -> tuple[Path, ...]:
            return tuple(
                replacement if position == facade_index else path
                for position, path in enumerate(sources)
            )

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            native = reclass.classifier_rules_fingerprint(workspace)
            baseline_path = workspace / "baseline.py"
            baseline_path.write_text(source, encoding="utf-8")
            comment_path = workspace / "comment.py"
            comment_path.write_text(source + "\n# trailing comment only\n", encoding="utf-8")
            changed_path = workspace / "changed.py"
            changed_path.write_text(source.replace('"Junk"', '"Junk2"'), encoding="utf-8")
            with patch.object(
                reclass, "_CLASSIFIER_MODULE_PATHS", _with_facade_replaced(baseline_path)
            ):
                baseline = reclass.classifier_rules_fingerprint(workspace)
            with patch.object(
                reclass, "_CLASSIFIER_MODULE_PATHS", _with_facade_replaced(comment_path)
            ):
                comment = reclass.classifier_rules_fingerprint(workspace)
            with patch.object(
                reclass, "_CLASSIFIER_MODULE_PATHS", _with_facade_replaced(changed_path)
            ):
                changed = reclass.classifier_rules_fingerprint(workspace)
        # Identical content at a different host path must not move the fingerprint.
        self.assertEqual(native, baseline)
        # A comment/formatting-only change must not move it; a rule change must.
        self.assertEqual(baseline, comment)
        self.assertNotEqual(baseline, changed)

    def test_caller_namespace_cannot_set_the_final_revision(self) -> None:
        item = _item()
        evaluate = Mock(return_value=_ready())
        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary, [item], [_email()],
                config={"attachment_run_id": "caller-ns"},
                evaluate=evaluate, reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=b"raw"),
            )
            fingerprint = reclass.classifier_rules_fingerprint(Path(temporary))
        installed = item["attachment_evaluation"]
        self.assertEqual(
            reclass.compute_classifier_revision(fingerprint, [_ATTACHMENT_SHA]),
            installed["classifier_revision"],
        )
        self.assertNotEqual("caller-ns", installed["classifier_revision"])
        self.assertTrue(str(evaluate.call_args.kwargs["run_id"]).startswith("caller-ns_"))

    def test_derive_run_id_is_deterministic_pii_free_and_isolated(self) -> None:
        identity = {
            "account": "primary-account-xyz", "folder": "INBOX",
            "envelope_id": "1", "message_id": "secret@example.test",
        }
        run_id = reclass.derive_evaluation_run_id(identity)
        self.assertTrue(afetch.is_valid_run_id(run_id))
        self.assertNotIn("secret", run_id)
        self.assertNotIn("primary-account-xyz", run_id)
        self.assertEqual(run_id, reclass.derive_evaluation_run_id(dict(identity)))
        other = dict(identity, message_id="other@example.test")
        self.assertNotEqual(run_id, reclass.derive_evaluation_run_id(other))
        namespaced = reclass.derive_evaluation_run_id(identity, namespace="base")
        self.assertTrue(namespaced.startswith("base_"))
        self.assertNotEqual(
            namespaced, reclass.derive_evaluation_run_id(other, namespace="base")
        )


_LEASE = "lease-mde2-t02"
_CONV = "conv-mde2-t02"


class Mde2NoDoubleFetchTests(unittest.TestCase):
    """A repeated default draft invocation reuses MD-E1 ``already_fetched``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name
        guard = afetch._load_workspace_lock_guard()
        guard.acquire_workspace_lock(
            self.workspace, harness="mde2-t02-test", lease_id=_LEASE, conversation_id=_CONV
        )
        self.addCleanup(
            lambda: guard._invoke(
                guard._command("release", Path(self.workspace), "--lease-id", _LEASE),
                None,
            )
        )
        preflight = patch.object(
            afetch, "verify_no_tracked_quarantine", return_value=None, create=True
        )
        preflight.start()
        self.addCleanup(preflight.stop)
        self.data_dir = Path(self.workspace) / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)

    def _config(self) -> dict[str, object]:
        return {
            "count": 1,
            "output_file": str(self.data_dir / "batch-manifest.json"),
            "lease_id": _LEASE,
            "conversation_id": _CONV,
        }

    def _dependencies(self, item: dict[str, object], email: dict[str, object], raw_eml: bytes):
        return {
            "BatchProgressTracker": _Tracker,
            "atomic_write_json": runner.atomic_write_json,
            "draft_manifest": _draft_with_sources([item]),
            "get_unprocessed_emails": Mock(return_value=([email], 0)),
            "load_sent_index": Mock(return_value={}),
            "classify_email": Mock(return_value=_clear_replacement()),
            "fetch_raw_message_eml": Mock(return_value=raw_eml),
        }

    def test_second_default_invocation_reaches_already_fetched(self) -> None:
        message_id = f"mde2-t02-{uuid.uuid4().hex[:10]}@example.test"
        envelope_id = "9001"
        raw_eml = attachments.build_test_eml(
            subject="MDE2 T02",
            message_id=f"<{message_id}>",
            attachments=[{
                "filename": "clue.txt",
                "mime_type": "text/plain",
                "data": _ATTACHMENT_TEXT.encode("utf-8"),
            }],
        )
        identity = {
            "account": "primary", "folder": "INBOX",
            "envelope_id": envelope_id, "message_id": message_id,
        }
        expected_run_id = reclass.derive_evaluation_run_id(identity)

        real_fetch = aevaluate.op_attachment_fetch
        statuses: list[str] = []

        def _recording_fetch(*args, **kwargs):
            result = real_fetch(*args, **kwargs)
            statuses.append(result["status"])
            return result

        first_item = _item(envelope_id=envelope_id, message_id=message_id)
        second_item = _item(envelope_id=envelope_id, message_id=message_id)
        email = _email(envelope_id=envelope_id, message_id=message_id)

        with patch.object(aevaluate, "op_attachment_fetch", side_effect=_recording_fetch):
            draft_mode.run_draft_mode(
                self._config(), account="primary", data_dir=self.data_dir,
                dependencies=self._dependencies(first_item, email, raw_eml),
            )
            draft_mode.run_draft_mode(
                self._config(), account="primary", data_dir=self.data_dir,
                dependencies=self._dependencies(second_item, email, raw_eml),
            )

        self.assertEqual(["fetched", "already_fetched"], statuses)
        for item in (first_item, second_item):
            installed = item["attachment_evaluation"]
            self.assertEqual("completed", installed["status"])
            self.assertEqual("classification_clear", installed["reason"])
            self.assertIs(True, installed["used_for_classification"])
            self.assertEqual(expected_run_id, installed["files"][0]["run_id"])
        # Exactly one physical quarantine artifact exists; no duplicate fetch was written.
        quarantined = list((self.data_dir / "attachments").rglob("clue.txt"))
        self.assertEqual(1, len(quarantined))
        self.assertEqual(_ATTACHMENT_TEXT.encode("utf-8"), quarantined[0].read_bytes())
        persisted = (self.data_dir / "batch-manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(_ATTACHMENT_TEXT, persisted)
        self.assertNotIn("prompt_content", persisted)
        self.assertNotIn("capability", persisted)
        self.assertNotIn("approval_receipt", persisted)
        self.assertNotRegex(persisted, _ABS_PATH_RE)


if __name__ == "__main__":
    unittest.main()
