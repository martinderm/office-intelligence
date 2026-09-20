"""FR-15 / MD-E2-T03 tests: opt-in ``inspect`` manifest proposals and batch isolation.

These tests fix the T03 public seam ``run_inspect_mode(config, account, data_dir,
dependencies)``.  Plain ``inspect`` must stay inspection-only; only the explicit
``evaluate_attachments`` opt-in may implicitly build a non-executable
``manifest_proposal`` that reuses the already-hardened MD-E2 ``draft`` item flow
(initial classification first, exactly one evaluation/reclassification per eligible
item, exactly one final ``attachment_evaluation`` per proposal item).

Every external boundary is injected through ``dependencies`` and only the persisted
JSON and the observable boundary calls are asserted; no private helper is asserted and
no live mailbox is required.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_batch_runner as runner  # noqa: E402
from core import attachment_reclassification as reclass  # noqa: E402
from core.modes import inspect as inspect_mode  # noqa: E402
import mde2_fixtures as fixtures  # noqa: E402

_ATTACHMENT_TEXT = fixtures.DEFAULT_TEXT
_RAW_MIME_MARKER = b"raw-rfc822-marker"
_ABS_PATH_RE = r"[A-Za-z]:\\\\"


# ==============================================================================
# Canonical fixtures (MD-E1-shaped handoffs via ``mde2_fixtures``)
# ==============================================================================

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


def _item(
    envelope_id: str,
    message_id: str,
    decision: dict[str, object],
    *,
    action: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "envelope_id": envelope_id,
        "source_folder": "INBOX",
        "message_id": message_id,
        "decision": decision,
        "action": action or {"type": "keep_in_folder", "target_folder": "INBOX"},
    }


def _email(envelope_id: str, message_id: str) -> dict[str, object]:
    return {"envelope_id": envelope_id, "folder": "INBOX", "message_id": message_id}


def _ready(envelope_id: str, message_id: str, decision: dict[str, object]) -> dict[str, object]:
    return fixtures.canonical_ready_evaluation(
        account="primary",
        folder="INBOX",
        envelope_id=envelope_id,
        message_id=message_id,
        decision=decision,
        text=_ATTACHMENT_TEXT,
    )


def _failed(reason: str) -> dict[str, object]:
    return {
        "attachment_evaluation": {
            "status": "failed",
            "reason": reason,
            "authorization": "not_applicable",
            "files": [],
            "used_for_classification": False,
            "classifier_revision": None,
        }
    }


def _draft_with_sources(
    items: list[dict[str, object]],
    transform=None,
    events: list[str] | None = None,
):
    """Stand-in for ``draft_manifest`` that reports each item's effective source.

    It echoes the inspected emails through the transient ``source_sink`` (optionally
    transformed to model a full-read effective source) and returns the fixed items.
    """

    def _draft(emails_arg, *, source_sink=None, **_kwargs):
        if events is not None:
            events.append("initial_classification")
        if source_sink is not None:
            for email in emails_arg:
                source_sink(transform(email) if transform else email)
        return {"items": items}

    return _draft


def _run_inspect(
    temporary: str,
    *,
    items: list[dict[str, object]],
    emails: list[dict[str, object]],
    evaluate: Mock,
    reclassify: Mock,
    raw_reader: Mock,
    config: dict[str, object] | None = None,
    transform=None,
    events: list[str] | None = None,
    draft: Mock | None = None,
    run_himalaya: Mock | None = None,
) -> tuple[dict[str, object], Path, Path]:
    data_dir = Path(temporary) / "data" / "mail-desk"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = data_dir / "batch-inspected.json"
    email_map = {str(email["envelope_id"]): email for email in emails}
    merged: dict[str, object] = {
        "envelope_ids": [str(email["envelope_id"]) for email in emails],
        "output_file": str(output_path),
    }
    merged.update(config or {})
    draft_dependency = (
        draft if draft is not None else _draft_with_sources(items, transform, events)
    )
    result = inspect_mode.run_inspect_mode(
        merged,
        account="primary",
        data_dir=data_dir,
        dependencies={
            "atomic_write_json": runner.atomic_write_json,
            "draft_manifest": draft_dependency,
            "get_oldest_envelopes": Mock(),
            "get_single_email_details": Mock(
                side_effect=lambda envelope_id, *_args, **_kwargs: email_map[str(envelope_id)]
            ),
            "get_unprocessed_emails": Mock(),
            "load_final_index": Mock(return_value={"items": {}}),
            "resolve_data_dir": Mock(return_value=data_dir),
            "resolve_final_index_path": Mock(return_value=data_dir / "index.json"),
            "run_himalaya": run_himalaya if run_himalaya is not None else Mock(),
            "sleep": Mock(),
            "attachment_evaluate": evaluate,
            "classify_email": reclassify,
            "fetch_raw_message_eml": raw_reader,
        },
    )
    return result, output_path, data_dir


# ==============================================================================
# T03-1 / T03-5: plain inspect is inspection-only; explicit proposal is unchanged
# ==============================================================================

class Mde2InspectInspectionOnlyTests(unittest.TestCase):
    """Omitted/default-false evaluation performs no attachment work at all."""

    def _emails(self) -> list[dict[str, object]]:
        return [_email("3", "three@example.test"), _email("1", "one@example.test")]

    def test_plain_inspect_is_inspection_only(self) -> None:
        evaluate = Mock()
        reclassify = Mock()
        raw_reader = Mock()
        draft = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result, output_path, _data_dir = _run_inspect(
                temporary,
                items=[],
                emails=self._emails(),
                evaluate=evaluate,
                reclassify=reclassify,
                raw_reader=raw_reader,
                draft=draft,
            )
            persisted = json.loads(output_path.read_text(encoding="utf-8"))

        evaluate.assert_not_called()
        reclassify.assert_not_called()
        raw_reader.assert_not_called()
        draft.assert_not_called()
        self.assertNotIn("manifest_proposal", result)
        self.assertNotIn("manifest_proposal", persisted)
        self.assertEqual("inspect", result["mode"])
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["total_inspected"])
        self.assertEqual(["3", "1"], [email["envelope_id"] for email in result["emails"]])

    def test_explicit_propose_manifest_without_evaluation_still_yields_proposal(self) -> None:
        item = _item("3", "three@example.test", _clear_decision())
        evaluate = Mock()
        reclassify = Mock()
        raw_reader = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result, _output_path, _data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[_email("3", "three@example.test")],
                config={"propose_manifest": True},
                evaluate=evaluate,
                reclassify=reclassify,
                raw_reader=raw_reader,
            )

        evaluate.assert_not_called()
        reclassify.assert_not_called()
        raw_reader.assert_not_called()
        self.assertIn("manifest_proposal", result)
        self.assertEqual([item], result["manifest_proposal"]["items"])


# ==============================================================================
# T03-2: CLI opt-in is observable through config + mode
# ==============================================================================

class Mde2InspectCliTests(unittest.TestCase):
    """Direct ``--inspect`` resolves the MD-E2 per-mode evaluation default."""

    def _config(self, argv: list[str]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            config = runner._direct_mode_config(
                runner._build_parser().parse_args(argv), Path(temporary)
            )
        assert config is not None
        return config

    def test_inspect_defaults_off_and_opt_in_flag_is_boolean_and_moded(self) -> None:
        default = self._config(["--inspect", "5"])
        self.assertIs(False, default["evaluate_attachments"])
        self.assertEqual("inspect", default["mode"])
        self.assertNotIn("min_confidence", default)

        opt_in = self._config(["--inspect", "5", "--evaluate-attachments"])
        self.assertIs(True, opt_in["evaluate_attachments"])
        self.assertEqual("inspect", opt_in["mode"])


# ==============================================================================
# T03-2 / T03-3: opt-in implicitly builds a proposal through the shared contract
# ==============================================================================

class Mde2InspectOptInProposalTests(unittest.TestCase):
    """``evaluate_attachments: true`` implicitly returns a ``manifest_proposal``."""

    def test_opt_in_creates_manifest_proposal_and_retains_inspection_fields(self) -> None:
        item = _item("2", "ambiguous@example.test", _ambiguous_decision())
        email = _email("2", "ambiguous@example.test")

        def evaluate(**kwargs):
            return _ready(str(kwargs["envelope_id"]), str(kwargs["message_id"]), kwargs["decision"])

        with tempfile.TemporaryDirectory() as temporary:
            result, output_path, _data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[email],
                config={"evaluate_attachments": True},
                evaluate=Mock(side_effect=evaluate),
                reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
            )
            persisted = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertIn("manifest_proposal", result)
        self.assertIn("manifest_proposal", persisted)
        # Top-level inspection fields and order survive the additive proposal.
        for field in ("ok", "mode", "folder", "order", "total_inspected", "known_count", "emails"):
            self.assertIn(field, result)
        self.assertEqual("inspect", result["mode"])
        self.assertEqual(1, result["total_inspected"])
        self.assertEqual(["2"], [entry["envelope_id"] for entry in result["emails"]])
        self.assertEqual(
            ["2"],
            [entry["envelope_id"] for entry in result["manifest_proposal"]["items"]],
        )
        self.assertEqual(result, persisted)


class Mde2InspectOrchestrationContractTests(unittest.TestCase):
    """Opt-in inspect reuses the T01/T02 one-pass, effective-source contract."""

    def test_proposal_runs_initial_classification_then_one_evaluation_and_reclassify(self) -> None:
        events: list[str] = []
        captured: dict[str, object] = {}
        item = _item("2", "ambiguous@example.test", _ambiguous_decision())

        def evaluate(**kwargs):
            events.append("evaluate")
            return _ready(str(kwargs["envelope_id"]), str(kwargs["message_id"]), kwargs["decision"])

        def reclassify(source, **kwargs):
            events.append("reclassify")
            captured["source"] = source
            captured["untrusted"] = kwargs.get("untrusted_external_text")
            return _clear_replacement()

        def transform(email):
            return dict(email, preview="FULL-BODY-CONTEXT-SIGNAL")

        evaluate_mock = Mock(side_effect=evaluate)
        reclassify_mock = Mock(side_effect=reclassify)
        raw_reader = Mock(return_value=_RAW_MIME_MARKER)
        with tempfile.TemporaryDirectory() as temporary:
            result, _output_path, _data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[_email("2", "ambiguous@example.test")],
                config={"evaluate_attachments": True},
                transform=transform,
                events=events,
                evaluate=evaluate_mock,
                reclassify=reclassify_mock,
                raw_reader=raw_reader,
            )

        # Initial (preview/full-read) classification strictly precedes evaluation.
        self.assertEqual(["initial_classification", "evaluate", "reclassify"], events)
        evaluate_mock.assert_called_once()
        reclassify_mock.assert_called_once()
        raw_reader.assert_called_once_with("2", folder="INBOX", account="primary")
        # The reclassification consumed the effective source, not the raw inspected email.
        self.assertEqual("FULL-BODY-CONTEXT-SIGNAL", captured["source"]["preview"])
        self.assertEqual(
            _ready("2", "ambiguous@example.test", _ambiguous_decision())
            ["attachment_analysis_handoff"]["prompt_content"],
            captured["untrusted"],
        )
        proposal_items = result["manifest_proposal"]["items"]
        self.assertEqual(1, len(proposal_items))
        self.assertEqual(
            {
                "status",
                "reason",
                "authorization",
                "files",
                "used_for_classification",
                "classifier_revision",
            },
            set(proposal_items[0]["attachment_evaluation"]),
        )
        self.assertEqual("completed", proposal_items[0]["attachment_evaluation"]["status"])
        self.assertEqual(
            "classification_clear", proposal_items[0]["attachment_evaluation"]["reason"]
        )


# ==============================================================================
# T03-4: executable manifest only on an explicit pre-existing manifest path
# ==============================================================================

class Mde2InspectManifestFileBoundaryTests(unittest.TestCase):
    """Merely enabling evaluation never writes an executable batch manifest."""

    def _ambiguous(self) -> tuple[dict[str, object], dict[str, object]]:
        return (
            _item("2", "ambiguous@example.test", _ambiguous_decision()),
            _email("2", "ambiguous@example.test"),
        )

    def _evaluate(self):
        return Mock(
            side_effect=lambda **kwargs: _ready(
                str(kwargs["envelope_id"]), str(kwargs["message_id"]), kwargs["decision"]
            )
        )

    def test_opt_in_without_manifest_path_writes_only_the_inspect_result(self) -> None:
        item, email = self._ambiguous()
        with tempfile.TemporaryDirectory() as temporary:
            result, output_path, data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[email],
                config={"evaluate_attachments": True},
                evaluate=self._evaluate(),
                reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
            )
            self.assertFalse((data_dir / "batch-manifest.json").exists())
            self.assertIn("manifest_proposal", result)
            self.assertNotIn("manifest_file_created", result)
            self.assertEqual(result, json.loads(output_path.read_text(encoding="utf-8")))

    def test_explicit_manifest_path_is_written_when_evaluation_is_enabled(self) -> None:
        item, email = self._ambiguous()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "nested" / "manifest.json"
            result, _output_path, _data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[email],
                config={
                    "evaluate_attachments": True,
                    "manifest_file": str(manifest_path),
                },
                evaluate=self._evaluate(),
                reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
            )
            self.assertTrue(manifest_path.exists())
            written = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(str(manifest_path.resolve()), result["manifest_file_created"])
        self.assertEqual(result["manifest_proposal"], written)
        self.assertIn("attachment_evaluation", written["items"][0])


# ==============================================================================
# T03-5: explicit proposal with evaluation disabled installs skipped terminals
# ==============================================================================

class Mde2InspectProposeWithoutEvaluationTests(unittest.TestCase):
    """A proposal emitted while evaluation is disabled still carries one terminal field."""

    def test_explicit_proposal_without_evaluation_installs_skipped_on_every_item(self) -> None:
        items = [
            _item("1", "one@example.test", _clear_decision()),
            _item("2", "ambiguous@example.test", _ambiguous_decision()),
        ]
        emails = [
            _email("1", "one@example.test"),
            _email("2", "ambiguous@example.test"),
        ]
        evaluate = Mock()
        reclassify = Mock()
        raw_reader = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result, _output_path, _data_dir = _run_inspect(
                temporary,
                items=items,
                emails=emails,
                config={"propose_manifest": True},
                evaluate=evaluate,
                reclassify=reclassify,
                raw_reader=raw_reader,
            )

        evaluate.assert_not_called()
        reclassify.assert_not_called()
        raw_reader.assert_not_called()
        self.assertIn("manifest_proposal", result)
        for item in result["manifest_proposal"]["items"]:
            self.assertIn("attachment_evaluation", item)
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


# ==============================================================================
# T03-6: mixed batch preserves order and isolates per-item outcomes
# ==============================================================================

class Mde2InspectMixedBatchTests(unittest.TestCase):
    """Clear, clarified, still-ambiguous and failed items stay isolated and ordered."""

    _CLEAR_SHA = fixtures.DEFAULT_TEXT

    def test_mixed_batch_preserves_order_and_isolates_outcomes(self) -> None:
        items = [
            _item("1", "clear@example.test", _clear_decision()),
            _item("2", "clarified@example.test", _ambiguous_decision()),
            _item("3", "ambiguous@example.test", _ambiguous_decision()),
            _item("4", "failed@example.test", _ambiguous_decision()),
        ]
        emails = [
            _email("1", "clear@example.test"),
            _email("2", "clarified@example.test"),
            _email("3", "ambiguous@example.test"),
            _email("4", "failed@example.test"),
        ]

        def evaluate(**kwargs):
            envelope_id = str(kwargs["envelope_id"])
            if envelope_id in {"2", "3"}:
                return _ready(envelope_id, str(kwargs["message_id"]), kwargs["decision"])
            return _failed("policy_blocked")

        def reclassify(source, **_kwargs):
            if str(source.get("envelope_id")) == "2":
                return _clear_replacement()
            return {"decision": _ambiguous_decision(), "action": {}}

        evaluate_mock = Mock(side_effect=evaluate)
        with tempfile.TemporaryDirectory() as temporary:
            result, _output_path, _data_dir = _run_inspect(
                temporary,
                items=items,
                emails=emails,
                config={"evaluate_attachments": True},
                evaluate=evaluate_mock,
                reclassify=Mock(side_effect=reclassify),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
            )

        self.assertIn("manifest_proposal", result)
        proposal_items = result["manifest_proposal"]["items"]
        self.assertEqual(
            ["1", "2", "3", "4"],
            [entry["envelope_id"] for entry in proposal_items],
        )
        # Only the three ambiguous items may reach the evaluator, in input order.
        self.assertEqual(
            ["2", "3", "4"],
            [str(call.kwargs["envelope_id"]) for call in evaluate_mock.call_args_list],
        )

        clear_eval = proposal_items[0]["attachment_evaluation"]
        self.assertEqual("not_needed", clear_eval["status"])
        self.assertEqual("classification_clear", clear_eval["reason"])
        self.assertIs(False, clear_eval["used_for_classification"])
        self.assertIsNone(clear_eval["classifier_revision"])
        self.assertEqual("high", proposal_items[0]["decision"]["confidence"])

        clarified_eval = proposal_items[1]["attachment_evaluation"]
        self.assertEqual("completed", clarified_eval["status"])
        self.assertEqual("classification_clear", clarified_eval["reason"])
        self.assertIs(True, clarified_eval["used_for_classification"])
        self.assertRegex(clarified_eval["classifier_revision"], r"^[0-9a-f]{64}$")
        self.assertEqual("project", proposal_items[1]["decision"]["kind"])
        self.assertEqual("Projekte/Pilot", proposal_items[1]["action"]["target_folder"])

        ambiguous_eval = proposal_items[2]["attachment_evaluation"]
        self.assertEqual("completed", ambiguous_eval["status"])
        self.assertEqual("still_ambiguous", ambiguous_eval["reason"])
        self.assertIs(False, ambiguous_eval["used_for_classification"])
        self.assertIsNone(ambiguous_eval["classifier_revision"])
        self.assertEqual(
            {"type": "keep_in_folder", "target_folder": "INBOX"}, proposal_items[2]["action"]
        )
        self.assertIs(True, proposal_items[2]["decision"]["review_required"])
        self.assertEqual("low", proposal_items[2]["decision"]["confidence"])

        failed_eval = proposal_items[3]["attachment_evaluation"]
        self.assertEqual("failed", failed_eval["status"])
        self.assertEqual("policy_blocked", failed_eval["reason"])
        self.assertIs(False, failed_eval["used_for_classification"])
        self.assertIsNone(failed_eval["classifier_revision"])
        self.assertEqual(
            {"type": "keep_in_folder", "target_folder": "INBOX"}, proposal_items[3]["action"]
        )
        self.assertIs(True, proposal_items[3]["decision"]["review_required"])
        self.assertEqual("low", proposal_items[3]["decision"]["confidence"])


# ==============================================================================
# T03-7: stable per-message run-id, no second cache
# ==============================================================================

class Mde2InspectRunIdStabilityTests(unittest.TestCase):
    """Repeated opt-in inspect reuses the deterministic per-message run-id."""

    def test_repeated_opt_in_inspect_reuses_stable_run_id(self) -> None:
        identity = {
            "account": "primary",
            "folder": "INBOX",
            "envelope_id": "2",
            "message_id": "ambiguous@example.test",
        }
        expected_run_id = reclass.derive_evaluation_run_id(identity)
        evaluate = Mock(
            side_effect=lambda **kwargs: _ready(
                str(kwargs["envelope_id"]), str(kwargs["message_id"]), kwargs["decision"]
            )
        )

        def run_once():
            return _run_inspect(
                temporary,
                items=[_item("2", "ambiguous@example.test", _ambiguous_decision())],
                emails=[_email("2", "ambiguous@example.test")],
                config={"evaluate_attachments": True},
                evaluate=evaluate,
                reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
            )

        with tempfile.TemporaryDirectory() as temporary:
            run_once()
            self.assertEqual(1, evaluate.call_count, "opt-in inspect must evaluate the item")
            first_run_id = evaluate.call_args.kwargs["run_id"]
            run_once()
            second_run_id = evaluate.call_args.kwargs["run_id"]
            self.assertFalse((Path(temporary) / "data" / "mail-desk" / "attachments").exists())

        self.assertEqual(expected_run_id, first_run_id)
        self.assertEqual(first_run_id, second_run_id)


# ==============================================================================
# T03-8: no raw content, capability, absolute path or mailbox mutation
# ==============================================================================

class Mde2InspectNoLeakNoMutationTests(unittest.TestCase):
    """Persisted inspect output and the proposal carry only bounded metadata."""

    def test_persisted_output_and_proposal_leak_nothing_and_write_no_mailbox(self) -> None:
        item = _item("2", "ambiguous@example.test", _ambiguous_decision())
        evaluate = Mock(
            side_effect=lambda **kwargs: _ready(
                str(kwargs["envelope_id"]), str(kwargs["message_id"]), kwargs["decision"]
            )
        )
        run_himalaya = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result, output_path, _data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[_email("2", "ambiguous@example.test")],
                config={"evaluate_attachments": True},
                evaluate=evaluate,
                reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
                run_himalaya=run_himalaya,
            )
            persisted = output_path.read_text(encoding="utf-8")

        run_himalaya.assert_not_called()
        self.assertIn("manifest_proposal", result)
        self.assertNotIn(_ATTACHMENT_TEXT, persisted)
        self.assertNotIn("prompt_content", persisted)
        self.assertNotIn(_RAW_MIME_MARKER.decode("ascii"), persisted)
        self.assertNotIn("capability", persisted)
        self.assertNotRegex(persisted, _ABS_PATH_RE)

        proposal_text = json.dumps(result["manifest_proposal"])
        self.assertNotIn(_ATTACHMENT_TEXT, proposal_text)
        self.assertNotIn("prompt_content", proposal_text)
        self.assertNotIn("capability", proposal_text)
        self.assertNotRegex(proposal_text, _ABS_PATH_RE)


# ==============================================================================
# T03-9: opt-in inspect does not widen execution approvals
# ==============================================================================

class Mde2InspectNonExecutableTests(unittest.TestCase):
    """Inspect stays a read-only report; the proposal is never executed."""

    def test_inspect_does_not_widen_pipeline_or_execute_approvals(self) -> None:
        item = _item("2", "ambiguous@example.test", _ambiguous_decision())
        run_himalaya = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result, _output_path, _data_dir = _run_inspect(
                temporary,
                items=[item],
                emails=[_email("2", "ambiguous@example.test")],
                config={"evaluate_attachments": True},
                evaluate=Mock(
                    side_effect=lambda **kwargs: _ready(
                        str(kwargs["envelope_id"]), str(kwargs["message_id"]), kwargs["decision"]
                    )
                ),
                reclassify=Mock(return_value=_clear_replacement()),
                raw_reader=Mock(return_value=_RAW_MIME_MARKER),
                run_himalaya=run_himalaya,
            )

        self.assertEqual("inspect", result["mode"])
        self.assertNotIn("approvals", result)
        self.assertNotIn("approval_receipt", result)
        self.assertNotIn("executed", result)
        self.assertNotIn("results", result)
        run_himalaya.assert_not_called()


if __name__ == "__main__":
    unittest.main()
