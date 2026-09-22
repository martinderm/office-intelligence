"""MD-R7 behavior tests for verify scope, runner provenance and the evidence fallback.

The tests reproduce B-9 (mixed-batch subset semantics), the runner-envelope
provenance gap and B-10 (canonical evidence fallback) while pinning the exact
match and explicit-evidence happy paths that must keep their current behavior.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes.verify import run_verify_mode  # noqa: E402
import verify_fixtures as fixtures  # noqa: E402


class VerifyScopeCharacterizationTests(unittest.TestCase):
    def test_exact_match_batch_releases_the_synthesis_handoff(self) -> None:
        """An execute candidate equal to the verified scope releases its handoff."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids()
            relative = fixtures.canonical_evidence_path()
            fixtures.write_evidence_document(root, relative, message_ids)
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)
            items = fixtures.items_with_evidence(
                [fixtures.review_item(message_id) for message_id in message_ids],
                relative,
            )
            results = fixtures.successful_results(items)
            candidate = fixtures.synthesis_candidate(items, results)
            summary = fixtures.execute_summary(items, results, candidate)

            result = run_verify_mode(
                {"items": items, "execute_summary": summary},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual("pending", result["synthesis_handoff"]["status"])
        self.assertEqual(len(message_ids), len(result["synthesis_handoff"]["items"]))
        self.assertEqual("completed", result["completion_report"]["status"])

    def test_candidate_id_outside_the_verified_scope_stays_not_required(self) -> None:
        """A candidate source outside the verified scope must never release."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids()
            foreign_id = "foreign-00@example.test"
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)
            items = [fixtures.review_item(message_id) for message_id in message_ids]
            results = fixtures.successful_results(items)
            candidate_items = [fixtures.review_item(message_id) for message_id in message_ids[:-1]]
            candidate_items.append(fixtures.review_item(foreign_id))
            candidate = fixtures.synthesis_candidate(candidate_items, fixtures.successful_results(candidate_items))
            summary = fixtures.execute_summary(items, results, candidate)

            result = run_verify_mode(
                {"items": items, "execute_summary": summary},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual("not_required", result["synthesis_handoff"]["status"])
        self.assertNotIn("completion_report", result)


class VerifyScopeSubsetTests(unittest.TestCase):
    def test_mixed_batch_with_candidate_subset_of_scope_releases_the_handoff(self) -> None:
        """A pending candidate whose ids are a subset of the verified scope releases."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids()
            relative = fixtures.canonical_evidence_path()
            fixtures.write_evidence_document(root, relative, message_ids)
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)
            items = fixtures.items_with_evidence(fixtures.mixed_batch_items(message_ids), relative)
            results = fixtures.successful_results(items)
            candidate = fixtures.synthesis_candidate(items[:-1], results[:-1])
            summary = fixtures.execute_summary(items, results, candidate)

            result = run_verify_mode(
                {"items": items, "execute_summary": summary},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual("pending", result["synthesis_handoff"]["status"])
        self.assertEqual(len(message_ids) - 1, len(result["synthesis_handoff"]["items"]))
        self.assertEqual("completed", result["completion_report"]["status"])
        self.assertNotIn(message_ids[-1], [item["message_id"] for item in result["synthesis_handoff"]["items"]])


class VerifyProvenanceTests(unittest.TestCase):
    def test_runner_envelope_data_mode_and_ok_is_accepted_as_provenance(self) -> None:
        """A saved runner execute envelope with data.mode/data.ok proves provenance."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids()
            relative = fixtures.canonical_evidence_path()
            fixtures.write_evidence_document(root, relative, message_ids)
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)
            items = fixtures.items_with_evidence(
                [fixtures.review_item(message_id) for message_id in message_ids],
                relative,
            )
            results = fixtures.successful_results(items)
            candidate = fixtures.synthesis_candidate(items, results)
            summary = fixtures.execute_summary(items, results, candidate)
            envelope_file = fixtures.write_json(
                data_dir / "execute-envelope.json",
                fixtures.runner_execute_envelope(summary),
            )

            result = run_verify_mode(
                {"batch_file": str(envelope_file)},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual("pending", result["synthesis_handoff"]["status"])
        self.assertEqual(len(message_ids), len(result["synthesis_handoff"]["items"]))
        self.assertEqual("completed", result["completion_report"]["status"])


class VerifyEvidenceFallbackTests(unittest.TestCase):
    def test_explicit_evidence_file_marks_in_evidence_true(self) -> None:
        """An item-bound evidence file marks in_evidence true."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids(count=2)
            relative = fixtures.canonical_evidence_path()
            fixtures.write_evidence_document(root, relative, message_ids)
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)
            items = fixtures.items_with_evidence(
                [fixtures.review_item(message_id) for message_id in message_ids],
                relative,
            )

            result = run_verify_mode(
                {"items": items},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual([True, True], [row["in_evidence"] for row in result["results"]])
        self.assertTrue(result["all_consistent"])

    def test_missing_canonical_evidence_is_false_and_marks_inconsistent(self) -> None:
        """Pure message ids without canonical evidence must fail the evidence check."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids(count=2)
            fixtures.empty_canonical_evidence_root(root)
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)

            result = run_verify_mode(
                {"message_ids": list(message_ids)},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual([False, False], [row["in_evidence"] for row in result["results"]])
        self.assertEqual([False, False], [row["consistent"] for row in result["results"]])
        self.assertFalse(result["all_consistent"])
        self.assertFalse(result["ok"])

    def test_canonical_evidence_files_mark_in_evidence_true(self) -> None:
        """The fallback reads the canonical memory/evidence layout."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_ids = fixtures.scope_ids(count=2)
            relative = fixtures.canonical_evidence_path()
            fixtures.write_evidence_document(root, relative, message_ids)
            fixtures.write_index(data_dir, message_ids)
            fixtures.write_action_log(data_dir, message_ids)

            result = run_verify_mode(
                {"message_ids": list(message_ids)},
                data_dir=data_dir,
                index_path=data_dir / fixtures.INDEX_FILENAME,
            )

        self.assertEqual([True, True], [row["in_evidence"] for row in result["results"]])
        self.assertTrue(result["all_consistent"])


if __name__ == "__main__":
    unittest.main()
