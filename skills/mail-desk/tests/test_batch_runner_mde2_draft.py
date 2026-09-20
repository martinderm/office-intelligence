"""FR-15 / MD-E2-T01 tests: default-on draft evaluation, one reclassification, CLI controls.

These tests fix the two confirmed T01 public seams: the Batch Runner CLI flags for direct
``draft``/``inspect`` and ``run_draft_mode`` through its injected external boundaries.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_batch_runner as runner  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
import mde2_fixtures as fixtures  # noqa: E402

_ATTACHMENT_TEXT = fixtures.DEFAULT_TEXT
_ATTACHMENT_SHA = hashlib.sha256(_ATTACHMENT_TEXT.encode("utf-8")).hexdigest()


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


def _ready_evaluation(
    *,
    envelope_id: str | int = "2",
    message_id: str = "ambiguous@example.test",
    account: str = "primary",
    folder: str = "INBOX",
    decision: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a canonical MD-E1-shaped ready evaluation (FR-15/MD-E2-T02 revalidation).

    T02 revalidates every ready handoff with ``validate_attachment_handoff``, so the fixture
    must be a genuine MD-E1 handoff bound to the item's trusted identity and decision.
    """
    return fixtures.canonical_ready_evaluation(
        account=account,
        folder=folder,
        envelope_id=envelope_id,
        message_id=message_id,
        decision=_ambiguous_decision() if decision is None else decision,
        text=_ATTACHMENT_TEXT,
    )


_PROMPT_CONTENT = _ready_evaluation()["attachment_analysis_handoff"]["prompt_content"]


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
    """Stand-in for draft_manifest that reports each item's effective source.

    Without a full read the effective source is the preview email, so the mock simply echoes
    the input emails through the transient sink before returning the fixed items.
    """

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
) -> tuple[dict[str, object], Path]:
    data_dir = Path(temporary) / "data" / "mail-desk"
    data_dir.mkdir(parents=True)
    output_path = data_dir / "batch-manifest.json"
    merged = {"count": len(emails), "output_file": str(output_path)}
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
    return result, output_path


class Mde2DraftIntegrationTests(unittest.TestCase):
    """run_draft_mode: initial classification first, MD-E1 only for ambiguous items."""

    def _clear_email(self) -> dict[str, object]:
        return {"envelope_id": "1", "folder": "INBOX", "message_id": "clear@example.test"}

    def _ambiguous_email(self) -> dict[str, object]:
        return {"envelope_id": "2", "folder": "INBOX", "message_id": "ambiguous@example.test"}

    def test_default_draft_evaluates_ambiguous_item_with_one_reclassification(self) -> None:
        item = {
            "envelope_id": "2",
            "message_id": "ambiguous@example.test",
            "decision": _ambiguous_decision(),
            "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
        }
        evaluate = Mock(return_value=_ready_evaluation())
        reclassify = Mock(
            return_value={
                "decision": _clear_decision(),
                "action": {"type": "copy_as_move", "target_folder": "Projekte/Pilot"},
                "notes": "reclassified by attachment",
                "evidence": {"file": "memory/references/projects/pilot/evidence/2026-09.md"},
                "synthesis_targets": [],
            }
        )
        raw_reader = Mock(return_value=b"raw-rfc822")

        with tempfile.TemporaryDirectory() as temporary:
            result, output_path = _run_draft(
                temporary,
                [item],
                [self._ambiguous_email()],
                evaluate=evaluate,
                reclassify=reclassify,
                raw_reader=raw_reader,
            )
            self.assertTrue(output_path.exists())

        evaluate.assert_called_once()
        self.assertEqual(_ambiguous_decision(), evaluate.call_args.kwargs["decision"])
        self.assertEqual(b"raw-rfc822", evaluate.call_args.kwargs["raw_eml"])
        self.assertEqual("2", evaluate.call_args.kwargs["envelope_id"])
        self.assertEqual("primary", evaluate.call_args.kwargs["account"])

        reclassify.assert_called_once()
        self.assertEqual(_PROMPT_CONTENT, reclassify.call_args.kwargs["untrusted_external_text"])

        installed = item["attachment_evaluation"]
        self.assertEqual(
            {
                "status",
                "reason",
                "authorization",
                "files",
                "used_for_classification",
                "classifier_revision",
            },
            set(installed),
        )
        self.assertEqual("completed", installed["status"])
        self.assertEqual("classification_clear", installed["reason"])
        self.assertEqual("auto_evaluated", installed["authorization"])
        self.assertEqual([_ready_evaluation()["attachment_evaluation"]["files"][0]], installed["files"])
        self.assertIs(True, installed["used_for_classification"])
        self.assertRegex(installed["classifier_revision"], r"^[0-9a-f]{64}$")
        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("Projekte/Pilot", item["action"]["target_folder"])
        # The draft result shape and the review contract must stay intact.
        self.assertEqual("draft", result["mode"])
        self.assertEqual(True, result["draft"]["review"]["required"])

        from core import attachment_reclassification as reclass

        fingerprint = reclass.classifier_rules_fingerprint(Path(temporary))
        self.assertEqual(
            reclass.compute_classifier_revision(fingerprint, [_ATTACHMENT_SHA]),
            installed["classifier_revision"],
        )

    def test_clear_item_is_not_needed_without_fetch_or_reclassification(self) -> None:
        item = {
            "envelope_id": "1",
            "message_id": "clear@example.test",
            "decision": _clear_decision(),
            "action": {"type": "copy_as_move", "target_folder": "Projekte/Pilot"},
        }
        evaluate = Mock()
        reclassify = Mock()
        raw_reader = Mock()

        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary,
                [item],
                [self._clear_email()],
                evaluate=evaluate,
                reclassify=reclassify,
                raw_reader=raw_reader,
            )

        evaluate.assert_not_called()
        reclassify.assert_not_called()
        raw_reader.assert_not_called()
        self.assertEqual(
            {
                "status": "not_needed",
                "reason": "classification_clear",
                "authorization": "not_applicable",
                "files": [],
                "used_for_classification": False,
                "classifier_revision": None,
            },
            item["attachment_evaluation"],
        )

    def test_disabled_evaluation_is_skipped_without_fetch_or_reclassification(self) -> None:
        item = {
            "envelope_id": "2",
            "message_id": "ambiguous@example.test",
            "decision": _ambiguous_decision(),
            "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
        }
        evaluate = Mock()
        reclassify = Mock()
        raw_reader = Mock()

        with tempfile.TemporaryDirectory() as temporary:
            _run_draft(
                temporary,
                [item],
                [self._ambiguous_email()],
                config={"evaluate_attachments": False},
                evaluate=evaluate,
                reclassify=reclassify,
                raw_reader=raw_reader,
            )

        evaluate.assert_not_called()
        reclassify.assert_not_called()
        raw_reader.assert_not_called()
        self.assertEqual(
            {
                "status": "skipped",
                "reason": "evaluation_disabled",
                "authorization": "not_applicable",
                "files": [],
                "used_for_classification": False,
                "classifier_revision": None,
            },
            item["attachment_evaluation"],
        )
        from core import attachment_reclassification as reclass

        self.assertIn(
            item["attachment_evaluation"]["reason"],
            reclass.ALLOWED_DRAFT_ATTACHMENT_EVALUATION_REASONS,
        )

    def test_non_boolean_evaluate_attachments_is_rejected_before_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                _run_draft(
                    temporary,
                    [],
                    [],
                    config={"evaluate_attachments": "yes"},
                    evaluate=Mock(),
                    reclassify=Mock(),
                    raw_reader=Mock(),
                )


class Mde2CliFlagTests(unittest.TestCase):
    """Direct CLI flags: mutual exclusion, per-mode defaults and mode scoping."""

    def _args(self, argv: list[str]):
        return runner._build_parser().parse_args(argv)

    def _config(self, argv: list[str]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            config = runner._direct_mode_config(self._args(argv), Path(temporary))
        assert config is not None
        return config

    def test_draft_defaults_evaluation_on_and_accepts_both_flags(self) -> None:
        self.assertIs(True, self._config(["--draft", "5"])["evaluate_attachments"])
        self.assertIs(True, self._config(["--draft", "5", "--evaluate-attachments"])["evaluate_attachments"])
        self.assertIs(
            False,
            self._config(["--draft", "5", "--no-evaluate-attachments"])["evaluate_attachments"],
        )

    def test_inspect_defaults_evaluation_off_and_accepts_opt_in(self) -> None:
        self.assertIs(False, self._config(["--inspect", "5"])["evaluate_attachments"])
        self.assertIs(
            True,
            self._config(["--inspect", "5", "--evaluate-attachments"])["evaluate_attachments"],
        )
        self.assertIs(
            False,
            self._config(["--inspect", "5", "--no-evaluate-attachments"])["evaluate_attachments"],
        )

    def test_flags_are_rejected_outside_direct_draft_or_inspect(self) -> None:
        with self.assertRaises(runner.ArgumentParseError):
            self._config(["--pipeline", "5", "--evaluate-attachments"])
        with self.assertRaises(runner.ArgumentParseError):
            self._config(["--sync-sent", "5", "--no-evaluate-attachments"])
        with self.assertRaises(runner.ArgumentParseError):
            self._config(["--evaluate-attachments"])

    def test_pipeline_config_is_unchanged(self) -> None:
        config = self._config(["--pipeline", "5"])
        self.assertEqual("pipeline", config["mode"])
        self.assertNotIn("evaluate_attachments", config)

    def test_both_flags_are_mutually_exclusive(self) -> None:
        with self.assertRaises(runner.ArgumentParseError):
            self._args(["--draft", "5", "--evaluate-attachments", "--no-evaluate-attachments"])


class Mde2DraftConfigValidationTests(unittest.TestCase):
    """Fix round 1: malformed draft config stops before any side effect."""

    _ITEM = {
        "envelope_id": "2",
        "source_folder": "INBOX",
        "message_id": "ambiguous@example.test",
        "decision": _ambiguous_decision(),
        "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
    }
    _EMAIL = {"envelope_id": "2", "folder": "INBOX", "message_id": "ambiguous@example.test"}

    def _assert_no_side_effects(self, data_dir: Path, config: dict[str, object]) -> None:
        writer = Mock()
        draft = Mock(return_value={"items": [dict(self._ITEM)]})
        evaluate = Mock(return_value=_ready_evaluation())
        reclassify = Mock(return_value={"decision": _clear_decision(), "action": {}})
        raw_reader = Mock(return_value=b"raw")
        fetch = Mock(return_value=([dict(self._EMAIL)], 0))
        with self.assertRaises(ValueError):
            draft_mode.run_draft_mode(
                config,
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _Tracker,
                    "atomic_write_json": writer,
                    "draft_manifest": draft,
                    "get_unprocessed_emails": fetch,
                    "load_sent_index": Mock(return_value={}),
                    "attachment_evaluate": evaluate,
                    "classify_email": reclassify,
                    "fetch_raw_message_eml": raw_reader,
                },
            )
        for spy in (draft, fetch, evaluate, reclassify, raw_reader, writer):
            spy.assert_not_called()

    def test_malformed_expected_count_performs_no_side_effects(self) -> None:
        for bad in ("5", 0, -1, True):
            with tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary) / "data" / "mail-desk"
                data_dir.mkdir(parents=True)
                self._assert_no_side_effects(data_dir, {"count": 1, "expected_count": bad})

    def test_non_boolean_allow_fewer_performs_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            self._assert_no_side_effects(data_dir, {"count": 1, "allow_fewer": "yes"})


class Mde2EffectiveSourceTests(unittest.TestCase):
    """Fix round 1: the single reclassification receives the effective full-read source."""

    _EMAIL = {
        "envelope_id": "9",
        "folder": "INBOX",
        "message_id": "full@example.test",
        "subject": "Frage",
        "from": "a@b.test",
        "to": "c@d.test",
        "date": "2026-09-01",
        "preview": "kurz",
    }
    _FULL_BODY = "VOLLER-BODY-KONTEXT-SIGNAL"

    def test_reclassification_consumes_effective_full_source_once(self) -> None:
        full_reader = Mock(
            return_value={
                "envelope_id": "9",
                "folder": "INBOX",
                "message_id": "full@example.test",
                "subject": "Frage",
                "from": "a@b.test",
                "to": "c@d.test",
                "date": "2026-09-01",
                "preview": self._FULL_BODY,
            }
        )
        evaluate = Mock(
            side_effect=lambda **kwargs: _ready_evaluation(
                envelope_id=kwargs["envelope_id"],
                message_id=kwargs["message_id"],
                account=kwargs["account"],
                folder=kwargs["folder"],
                decision=kwargs["decision"],
            )
        )
        captured: dict[str, object] = {}

        def reclassify(source, **kwargs):
            captured["source"] = source
            captured["untrusted"] = kwargs.get("untrusted_external_text")
            return {
                "decision": _clear_decision(),
                "action": {"type": "copy_as_move", "target_folder": "Projekte/Pilot"},
                "notes": "ok",
            }

        raw_reader = Mock(return_value=b"raw")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_path = data_dir / "batch-manifest.json"
            draft_mode.run_draft_mode(
                {"count": 1, "output_file": str(output_path)},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _Tracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "get_unprocessed_emails": Mock(return_value=([dict(self._EMAIL)], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "get_single_email_details": full_reader,
                    "attachment_evaluate": evaluate,
                    "classify_email": reclassify,
                    "fetch_raw_message_eml": raw_reader,
                },
            )
            persisted = output_path.read_text(encoding="utf-8")

        self.assertEqual(1, full_reader.call_count, "the full read must happen exactly once")
        self.assertEqual(self._FULL_BODY, captured["source"]["preview"])
        self.assertEqual(_PROMPT_CONTENT, captured["untrusted"])
        self.assertNotIn(self._FULL_BODY, persisted)
        self.assertNotIn("prompt_content", persisted)
        self.assertNotIn('"preview"', persisted)


class Mde2SourcePairingTests(unittest.TestCase):
    """Fix round 1: identity pairing fails closed instead of attaching a wrong source."""

    _ITEM = {
        "envelope_id": "1",
        "source_folder": "INBOX",
        "message_id": "m1",
        "decision": _ambiguous_decision(),
        "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
    }

    def _install(self, sources: list[dict[str, object]]) -> tuple[dict[str, object], Mock]:
        from core import attachment_reclassification as reclass
        from core import attachment_evaluation as evaluate_module

        item = dict(self._ITEM)
        evaluate = Mock(return_value=_ready_evaluation())
        with tempfile.TemporaryDirectory() as temporary:
            reclass.install_draft_attachment_evaluations(
                [item],
                sources,
                evaluate_attachments=True,
                workspace_root=Path(temporary),
                data_dir=Path(temporary) / "data" / "mail-desk",
                account="primary",
                evaluate=evaluate,
                reclassify=Mock(return_value={"decision": _clear_decision(), "action": {}}),
                read_raw_mime=Mock(return_value=b"raw"),
                triggers_evaluation=evaluate_module.decision_triggers_evaluation,
            )
        return item, evaluate

    def test_missing_identity_match_fails_closed(self) -> None:
        item, evaluate = self._install([{"envelope_id": "2", "folder": "INBOX"}])
        evaluate.assert_not_called()
        self.assertEqual("failed", item["attachment_evaluation"]["status"])
        self.assertIs(False, item["attachment_evaluation"]["used_for_classification"])

    def test_duplicate_identity_fails_closed(self) -> None:
        item, evaluate = self._install(
            [{"envelope_id": "1", "folder": "INBOX"}, {"envelope_id": "1", "folder": "INBOX"}]
        )
        evaluate.assert_not_called()
        self.assertEqual("failed", item["attachment_evaluation"]["status"])


if __name__ == "__main__":
    unittest.main()
