"""MD-H2 regression tests for the reviewed candidate-count gate."""

from pathlib import Path
from copy import deepcopy
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


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
            "verify_in_target_folder": Mock(return_value="verified"),
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

    def test_unbound_requests_stop_before_all_side_effects(self) -> None:
        for extra, account in (({}, None), ({"account": "primary"}, "primary")):
            with self.subTest(account=account):
                self.assert_gate_stops_all_mutations({
                    "mode": "execute", "items": [self.item("unbound")], **extra,
                }, "incomplete_contract", account=account)

    def test_each_missing_contract_field_stops_before_all_side_effects(self) -> None:
        for key in ("expected_count", "candidate_count", "allow_fewer", "source_folder", "account", "skip_known", "review"):
            with self.subTest(key=key):
                config = self.manifest(expected=1, actual=1)
                del config[key]
                self.assert_gate_stops_all_mutations(config, "incomplete_contract")

    def test_missing_and_invalid_receipts_stop_before_all_side_effects(self) -> None:
        for receipt in (None, {}, {"reviewed_by": "someone"}, {
            "reviewed_by": "", "reviewed_at": "now", "execute_request_sha256": "0" * 64,
        }):
            with self.subTest(receipt=receipt):
                config = self.manifest(expected=1, actual=1)
                config["review"]["approval_receipt"] = receipt
                self.assert_gate_stops_all_mutations(config, "invalid_review_receipt")

    def test_unbound_request_does_not_create_or_cleanup_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            sentinel = data_dir / "batch-recovery-journal.json"
            sentinel.write_bytes(b"preserve existing evidence")
            before = {p.relative_to(data_dir): p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}
            result = execute.run_execute_mode({"items": [self.item("unbound")]}, data_dir=data_dir)
            self.assertFalse(result["ok"])
            self.assertEqual(before, {p.relative_to(data_dir): p.read_bytes() for p in data_dir.rglob("*") if p.is_file()})

    def test_facade_rejects_unbound_before_readiness_or_handler(self) -> None:
        with patch.object(runner, "_bind_mailbox_mode", return_value=(Path("unused"), {"account": None}, None)), patch.object(
            runner, "mailbox_readiness_preflight", side_effect=AssertionError("mailbox access before approval"),
        ), patch.object(runner, "_run_execute_mode", side_effect=AssertionError("handler before approval")):
            result = runner.run_execute_mode({"items": [self.item("unbound")]})
        self.assertFalse(result["ok"])
        self.assertEqual("incomplete_contract", result["contract_gate"]["reason_code"])

    def test_missing_fields_have_contract_error_even_without_backend_configuration(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner, "run_himalaya") as mail:
            data_dir = Path(temporary) / "data" / "mail-desk"
            result = runner.run_execute_mode({"items": []}, data_dir=data_dir)
            self.assertEqual("incomplete_contract", result["contract_gate"]["reason_code"])
            self.assertFalse(data_dir.exists())
            mail.assert_not_called()

    def test_real_cli_rejects_invalid_manifests_without_changing_any_local_file(self):
        approved = self.manifest(expected=1, actual=1)
        cases = [({"mode": "execute", "items": [self.item("unbound")]}, "incomplete_contract")]
        for field in ("expected_count", "candidate_count", "allow_fewer", "source_folder", "account", "skip_known", "review"):
            incomplete = deepcopy(approved)
            del incomplete[field]
            cases.append((incomplete, "incomplete_contract"))
        for receipt, reason in ((None, "invalid_review_receipt"), ({}, "invalid_review_receipt"), ({
            "reviewed_at": "now", "reviewed_by": "human", "execute_request_sha256": "0" * 64,
        }, "review_hash_mismatch")):
            invalid = deepcopy(approved)
            invalid["review"]["approval_receipt"] = receipt
            cases.append((invalid, reason))
        for config, reason in cases:
            with self.subTest(reason=reason, keys=sorted(config)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                data_dir = root / "data" / "mail-desk"
                data_dir.mkdir(parents=True)
                manifest = data_dir / "batch-manifest.json"
                manifest.write_text(json.dumps(config), encoding="utf-8")
                (data_dir / "batch-recovery-journal.json").write_bytes(b"must not be opened")
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                result = subprocess.run(
                    [sys.executable, "-B", str(SCRIPTS / "mail_desk_batch_runner.py"),
                     "--input", str(manifest), "--data-dir", str(data_dir)],
                    cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(1, result.returncode, result.stderr)
                envelope = json.loads(result.stdout)
                self.assertFalse(envelope["success"])
                self.assertEqual(reason, envelope["data"]["contract_gate"]["reason_code"])
                self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

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
