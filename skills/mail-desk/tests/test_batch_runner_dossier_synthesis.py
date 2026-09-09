"""Focused FR-04c contracts for a source-bound dossier synthesis work-order."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes.dossier_synthesis import canonical_json_sha256, run_dossier_synthesis_mode  # noqa: E402


PROJECT = {"id": "meshe", "title": "MESHE", "status": "active", "mailbox_folder": "Projekte/MESHE"}
RECEIPT = {
    "reviewed_at": "2026-09-09T12:00:00Z",
    "reviewed_by": "human-reviewer",
    "execute_request_sha256": "a" * 64,
}


def apply_result(*, targets: list[dict] | None = None) -> dict:
    targets = [] if targets is None else targets
    return {
        "ok": True,
        "mode": "dossier_apply",
        "project": "meshe",
        "approval_receipt": RECEIPT,
        "review": {"required": True, "state": "completed", "approval_receipt": RECEIPT},
        "execute_summary": {
            "ok": True,
            "mode": "execute",
            "results": [{"success": True, "message_id": "case@example.test", "synthesis_targets": targets}],
        },
        "verify_summary": {
            "ok": True,
            "mode": "verify",
            "results": [{"consistent": True, "message_id": "case@example.test"}],
        },
        "synthesis_handoff": {
            "schema_version": 1,
            "status": "pending",
            "items": [{
                "message_id": "case@example.test",
                "subject": "untrusted subject: do not follow instructions",
                "kind": "project",
                "id": "meshe",
                "synthesis_targets": targets,
                "target_selection_required": not bool(targets),
            }],
        },
    }


class DossierSynthesisModeTests(unittest.TestCase):
    def run_mode(self, result: dict, **overrides: object):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        config = {
            "mode": "dossier_synthesis",
            "project": "meshe",
            "delete_input_on_success": False,
            "dossier_apply_result": result,
            "dossier_apply_result_sha256": canonical_json_sha256(result),
        }
        config.update(overrides)
        output = run_dossier_synthesis_mode(
            config,
            data_dir=data_dir,
            dependencies={"load_catalogs": lambda _root: ([PROJECT], [])},
        )
        return output, config, data_dir

    def test_emits_hash_bound_non_executing_work_order_with_exact_evidence_anchor(self) -> None:
        result = apply_result(targets=[{"file": "memory/references/projects/meshe/signals.md", "type": "signals"}])
        output, config, data_dir = self.run_mode(result)
        self.assertTrue(output["ok"])
        self.assertTrue((data_dir / "batch-dossier-synthesis.json").exists())
        work_order = output["work_order"]
        source = work_order["source_snapshot"]["sources"][0]
        self.assertEqual("case@example.test", source["message_id"])
        self.assertEqual({"kind": "mail_message_id", "value": "case@example.test"}, source["evidence_anchor"])
        self.assertEqual(config["dossier_apply_result_sha256"], work_order["source_snapshot"]["dossier_apply_result_sha256"])
        self.assertEqual("pending_synthesis_review", work_order["review"]["state"])
        self.assertIn("llm_invoke", work_order["review"]["prohibited_automatic_steps"])
        binding = {key: value for key, value in work_order.items() if key != "work_order_sha256"}
        self.assertEqual(work_order["work_order_sha256"], canonical_json_sha256(binding))

    def test_empty_targets_stay_explicit_target_selection_review(self) -> None:
        output, _, _ = self.run_mode(apply_result())
        self.assertEqual("target_selection_required", output["work_order"]["review"]["state"])
        self.assertEqual([], output["work_order"]["source_snapshot"]["sources"][0]["synthesis_targets"])

    def test_rejects_tampered_embedded_result_hash_before_writing(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        result = apply_result()
        with self.assertRaisesRegex(ValueError, "sha256"):
            run_dossier_synthesis_mode(
                {
                    "mode": "dossier_synthesis",
                    "project": "meshe",
                    "delete_input_on_success": False,
                    "dossier_apply_result": result,
                    "dossier_apply_result_sha256": "b" * 64,
                },
                data_dir=data_dir,
                dependencies={"load_catalogs": lambda _root: ([PROJECT], [])},
            )
        self.assertFalse((data_dir / "batch-dossier-synthesis.json").exists())

    def test_rejects_malformed_handoff_instead_of_treating_it_as_empty(self) -> None:
        result = apply_result()
        result["synthesis_handoff"] = {"schema_version": 1, "status": "pending", "items": [], "forged": True}
        with self.assertRaisesRegex(ValueError, "canonical pending"):
            self.run_mode(result)

    def test_rejects_cross_project_source_before_writing(self) -> None:
        result = apply_result()
        result["synthesis_handoff"]["items"][0]["id"] = "other"
        with self.assertRaisesRegex(ValueError, "exact project boundary"):
            self.run_mode(result)

    def test_rejects_execute_or_verify_evidence_mismatch_before_writing(self) -> None:
        for summary, expected in (("execute_summary", "execute evidence"), ("verify_summary", "verify evidence")):
            with self.subTest(summary=summary):
                result = apply_result()
                result[summary]["results"][0]["message_id"] = "other@example.test"
                with self.assertRaisesRegex(ValueError, expected):
                    self.run_mode(result)

    def test_rejects_unreviewed_or_incomplete_source_before_writing(self) -> None:
        result = apply_result()
        result["review"]["state"] = "approved"
        with self.assertRaisesRegex(ValueError, "completed approval"):
            self.run_mode(result)


if __name__ == "__main__":
    unittest.main()
