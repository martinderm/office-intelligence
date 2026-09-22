"""FR-17 / MD-R3 tests: deterministic MIME inventory chain and field consistency.

These behavior tests define the MD-R3 target semantics at the existing public seams
``run_draft_mode``, the real two-pass classifier and the real MD-E2 installation:

1. Exactly one canonical MIME export per item per run.  A transient timeout on the
   classifier export yields a defined, repeatable Review/``INBOX`` outcome and must not
   trigger a second, differently-timed export inside the same run (B-3).
2. ``attachments[]``/``attachment_status``/``attachment_error`` and
   ``attachment_evaluation``/``files[]`` are mutually consistent; a completed evaluation
   with a filled ``files[]`` must never coexist with ``attachment_inventory_unavailable``
   (B-4).
3. ``notes`` derive exclusively from the final decision; an ``unknown``/``unclassified``
   item carries no assignment wording (B-4).
4. The deterministic MD-E2 run-id and its reuse across runs stay pinned; the timeout case
   must not change the derived run-id (FR-15 idempotence surface).

The module is tests-only: no production, documentation or system-map file is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import attachment_reclassification as reclass  # noqa: E402
from core import attachments  # noqa: E402
from core import classifier  # noqa: E402
import mime_timeout_fixtures as fixtures  # noqa: E402
import routing_fixtures  # noqa: E402


def _workspace(temporary: str, name: str = "workspace") -> Path:
    workspace = Path(temporary) / name
    (workspace / "data" / "mail-desk").mkdir(parents=True, exist_ok=True)
    return workspace


def _write_topic_catalog(workspace: Path, topic: dict[str, object]) -> None:
    topics_path = workspace / "memory" / "references" / "topics" / "topics.json"
    topics_path.parent.mkdir(parents=True, exist_ok=True)
    topics_path.write_text(json.dumps({"topics": [topic]}), encoding="utf-8")


def _malformed_attachment() -> dict[str, object]:
    return {
        "filename": "clue.pdf",
        "mime_type": "",
        "size_bytes": 2048,
        "sha256": "0" * 64,
        "part_locator": "1",
        "provenance": attachments.PROVENANCE_RFC822,
    }


class TransientMimeTimeoutConsistencyTests(unittest.TestCase):
    """A transient export timeout yields one defined review item without a same-run fetch."""

    def _run_timeout(self, temporary: str, *, fail_forever: bool = False):
        reader = fixtures.FlakyMimeReader(
            fixtures.fixed_raw_eml(), fail_first_n=1, fail_forever=fail_forever
        )
        result, output_path = fixtures.run_draft_with_reader(
            workspace=_workspace(temporary), reader=reader, email=fixtures.ambiguous_email()
        )
        return reader, result, output_path

    def test_transient_export_timeout_does_not_reach_a_same_run_md_e2_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reader, result, _output = self._run_timeout(temporary)

        item = result["draft"]["items"][0]
        self.assertEqual(1, reader.call_count, "one canonical MIME export per item per run")
        self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
        self.assertTrue(item["decision"]["review_required"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        evaluation = item["attachment_evaluation"]
        self.assertNotEqual("completed", evaluation["status"])
        self.assertEqual([], evaluation["files"])

    def test_attachment_failed_item_never_pairs_completed_evaluation_with_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _reader, result, _output = self._run_timeout(temporary)

        item = result["draft"]["items"][0]
        evaluation = item["attachment_evaluation"]
        contradictory = (
            item["attachment_status"] == "attachment_inventory_unavailable"
            and evaluation["status"] == "completed"
            and len(evaluation["files"]) > 0
        )
        self.assertFalse(
            contradictory,
            "attachment_inventory_unavailable must not coexist with a completed files[] evaluation",
        )

    def test_permanent_export_timeout_is_a_bounded_review_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _reader, result, _output = self._run_timeout(temporary, fail_forever=True)

        item = result["draft"]["items"][0]
        self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
        self.assertTrue(item["decision"]["review_required"])
        evaluation = item["attachment_evaluation"]
        self.assertNotEqual("completed", evaluation["status"])
        self.assertEqual([], evaluation["files"])


class MimeInventoryDeterminismTests(unittest.TestCase):
    """Identical inputs and mailbox state must produce one byte-identical manifest."""

    def test_three_timeout_runs_are_byte_identical_and_defined(self) -> None:
        manifests: list[bytes] = []
        items: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary:
            for run_index in range(3):
                reader = fixtures.FlakyMimeReader(
                    fixtures.fixed_raw_eml(), fail_first_n=1
                )
                result, output_path = fixtures.run_draft_with_reader(
                    workspace=_workspace(temporary, f"run-{run_index}"),
                    reader=reader,
                    email=fixtures.ambiguous_email(),
                )
                manifests.append(output_path.read_bytes())
                items.append(result["draft"]["items"][0])

        self.assertEqual(
            1, len(set(manifests)), "identical inputs and mailbox state must yield one manifest"
        )
        for item in items:
            evaluation = item["attachment_evaluation"]
            self.assertNotEqual("completed", evaluation["status"])
            self.assertEqual([], evaluation["files"])


class EvaluationRunIdReuseTests(unittest.TestCase):
    """The deterministic, PII-free MD-E2 run-id is stable across the timeout case."""

    def _identity(self) -> dict[str, str]:
        return {
            "account": fixtures.ACCOUNT,
            "folder": fixtures.FOLDER,
            "envelope_id": fixtures.AMBIGUOUS_ENVELOPE_ID,
            "message_id": fixtures.AMBIGUOUS_MESSAGE_ID,
        }

    def test_derive_evaluation_run_id_is_deterministic_and_pii_free(self) -> None:
        identity = self._identity()
        run_id = reclass.derive_evaluation_run_id(identity)
        self.assertTrue(run_id.startswith("eval_"))
        self.assertEqual(run_id, reclass.derive_evaluation_run_id(dict(identity)))
        self.assertNotIn(fixtures.AMBIGUOUS_MESSAGE_ID, run_id)
        self.assertNotIn(fixtures.ACCOUNT, run_id)

    def test_timeout_then_recovery_reuses_the_same_derived_run_id(self) -> None:
        expected = reclass.derive_evaluation_run_id(self._identity())
        timeout_calls: list[dict[str, object]] = []
        recovery_calls: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _workspace(temporary)
            timeout_reader = fixtures.FlakyMimeReader(
                fixtures.fixed_raw_eml(), fail_first_n=1
            )
            fixtures.run_draft_with_reader(
                workspace=workspace,
                reader=timeout_reader,
                email=fixtures.ambiguous_email(),
                evaluate=fixtures.make_evaluate_stub(timeout_calls),
            )
            recovery_reader = fixtures.FlakyMimeReader(
                fixtures.fixed_raw_eml(), fail_first_n=0
            )
            fixtures.run_draft_with_reader(
                workspace=workspace,
                reader=recovery_reader,
                email=fixtures.ambiguous_email(),
                evaluate=fixtures.make_evaluate_stub(recovery_calls),
            )

        self.assertEqual(1, len(recovery_calls), "the recovered run reaches one MD-E2 evaluation")
        self.assertEqual(expected, recovery_calls[0]["run_id"])
        for call in timeout_calls:
            self.assertEqual(expected, call["run_id"])


class NotesFollowFinalDecisionTests(unittest.TestCase):
    """An unknown/unclassified item must not carry a topic assignment wording."""

    def _topic_email(self) -> dict[str, object]:
        return fixtures.ambiguous_email(
            message_id="mdr3-notes@example.test",
            subject="Hochschule International Newsletter 7/2026",
            preview="Newsletter der Hochschule International.",
        )

    def test_unknown_item_drops_topic_assignment_wording(self) -> None:
        topic = routing_fixtures.topic_hochschule_international()
        email = self._topic_email()
        email["attachments"] = [_malformed_attachment()]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _workspace(temporary)
            item = classifier.classify_email(
                email,
                workspace_root=workspace,
                projects=[],
                topics=[topic],
                sent_lookup={},
                final_index={"items": {}},
                account=fixtures.ACCOUNT,
            )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
        self.assertNotIn("Themenbezogene Zuordnung", item["notes"])

    def test_timeout_review_item_notes_do_not_claim_a_topic_assignment(self) -> None:
        topic = routing_fixtures.topic_hochschule_international()
        reader = fixtures.FlakyMimeReader(fixtures.fixed_raw_eml(), fail_first_n=1)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _workspace(temporary)
            _write_topic_catalog(workspace, topic)
            result, _output = fixtures.run_draft_with_reader(
                workspace=workspace, reader=reader, email=self._topic_email()
            )

        item = result["draft"]["items"][0]
        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
        self.assertNotIn("Themenbezogene Zuordnung", item["notes"])


if __name__ == "__main__":
    unittest.main()
