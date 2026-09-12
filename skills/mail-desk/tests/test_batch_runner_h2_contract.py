"""MD-H2 regression tests for the reviewed candidate-count gate."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core.batch_contract import add_draft_contract  # noqa: E402
from core.modes import execute  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402


class ReviewedCandidateContractTests(unittest.TestCase):
    @staticmethod
    def item(number: str) -> dict:
        return {
            "envelope_id": number,
            "source_folder": "INBOX",
            "message_id": f"case-{number}@example.test",
            "action": {"type": "keep_in_folder", "target_folder": "INBOX"},
            "decision": {},
        }

    def manifest(
        self, *, expected: int, actual: int, allow_fewer: bool = False,
        account: str | None = None,
    ) -> dict:
        config = {"mode": "execute", "items": [self.item(str(index)) for index in range(actual)]}
        add_draft_contract(
            config,
            expected_count=expected,
            allow_fewer=allow_fewer,
            source_folder="INBOX",
            account=account,
            skip_known=True,
        )
        reviewed_hash = config["review"].pop("execute_request_sha256")
        config["review"] = {
            "required": True,
            "state": "approved",
            "approval_receipt": {
                "reviewed_at": "2026-09-12T09:30:00Z",
                "reviewed_by": "reviewer@example.test",
                "execute_request_sha256": reviewed_hash,
            },
        }
        return config

    def execute(self, config: dict, *, account: str | None = None) -> tuple[dict, dict[str, Mock]]:
        dependencies = {
            "BatchProgressTracker": Mock(),
            "load_final_index": Mock(return_value={"items": {}}),
            "run_himalaya": Mock(),
            "append_action_log_entry": Mock(),
            "append_replies_needed_entry": Mock(),
            "flush_batch_evidence": Mock(),
            "save_final_index_atomic": Mock(),
            "auto_resolve_replies_from_sent": Mock(),
        }
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = execute.run_execute_mode(
                config, account=account, data_dir=data_dir, dependencies=dependencies,
            )
        return result, dependencies

    def assert_gate_stops_all_mutations(
        self, config: dict, code: str, *, account: str | None = None,
    ) -> None:
        result, dependencies = self.execute(config, account=account)
        self.assertFalse(result["ok"])
        self.assertEqual(code, result["contract_gate"]["reason_code"])
        self.assertEqual([], result["results"])
        for name in (
            "BatchProgressTracker", "run_himalaya", "append_action_log_entry",
            "append_replies_needed_entry", "flush_batch_evidence",
            "save_final_index_atomic", "auto_resolve_replies_from_sent",
        ):
            dependencies[name].assert_not_called()

    def test_exact_match_with_approved_manifest_executes(self) -> None:
        result, dependencies = self.execute(self.manifest(expected=2, actual=2))
        self.assertTrue(result["ok"])
        self.assertEqual("reviewed", result["contract_gate"]["state"])
        self.assertEqual(2, result["total_processed"])
        self.assertEqual(2, dependencies["append_action_log_entry"].call_count)

    def test_fewer_candidates_stop_by_default_before_execute(self) -> None:
        self.assert_gate_stops_all_mutations(self.manifest(expected=3, actual=2), "fewer_than_expected")

    def test_explicit_allow_fewer_permits_reviewed_subset(self) -> None:
        result, dependencies = self.execute(self.manifest(expected=3, actual=2, allow_fewer=True))
        self.assertTrue(result["ok"])
        self.assertEqual(2, dependencies["append_action_log_entry"].call_count)

    def test_more_than_expected_stops_even_when_allow_fewer(self) -> None:
        self.assert_gate_stops_all_mutations(self.manifest(expected=2, actual=3, allow_fewer=True), "more_than_expected")

    def test_pending_review_stops_before_all_side_effects(self) -> None:
        config = {"mode": "execute", "items": [self.item("pending")]}
        add_draft_contract(
            config, expected_count=1, allow_fewer=False, source_folder="INBOX",
            account=None, skip_known=True,
        )
        self.assert_gate_stops_all_mutations(config, "review_required")

    def test_mismatched_review_hash_stops_before_all_side_effects(self) -> None:
        config = self.manifest(expected=1, actual=1)
        config["review"]["approval_receipt"]["execute_request_sha256"] = "0" * 64
        self.assert_gate_stops_all_mutations(config, "review_hash_mismatch")

    def test_account_and_source_folder_drift_stop_before_all_side_effects(self) -> None:
        self.assert_gate_stops_all_mutations(
            self.manifest(expected=1, actual=1, account="primary"), "account_mismatch",
            account="secondary",
        )
        config = self.manifest(expected=1, actual=1)
        config["items"][0]["source_folder"] = "Other"
        self.assert_gate_stops_all_mutations(config, "source_folder_mismatch")

    def test_boolean_candidate_count_stops_before_all_side_effects(self) -> None:
        config = self.manifest(expected=1, actual=1)
        config["candidate_count"] = True
        self.assert_gate_stops_all_mutations(config, "invalid_candidate_count")

    def test_unbound_reviewed_path_with_account_remains_legacy_compatible(self) -> None:
        result, dependencies = self.execute({
            "mode": "execute", "account": "primary", "items": [self.item("legacy")],
        }, account="primary")
        self.assertTrue(result["ok"])
        self.assertEqual("legacy_unbound", result["contract_gate"]["state"])
        dependencies["append_action_log_entry"].assert_called_once()

    def test_draft_cli_exposes_exact_contract_and_rejects_orphan_flags(self) -> None:
        parser = runner._build_parser()
        args = parser.parse_args(["--draft", "3", "--expected-count", "3", "--allow-fewer"])
        config = runner._direct_mode_config(args, Path("C:/tmp/data"))
        self.assertEqual(3, config["expected_count"])
        self.assertTrue(config["allow_fewer"])
        with self.assertRaisesRegex(runner.ArgumentParseError, "require --draft"):
            runner._direct_mode_config(parser.parse_args(["--allow-fewer"]), Path("C:/tmp/data"))


if __name__ == "__main__":
    unittest.main()
