"""Focused FR-04d contracts for review-only dossier downstream handoffs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes.dossier_handoff import run_dossier_handoff_mode  # noqa: E402
from core.modes.dossier_synthesis import canonical_json_sha256, run_dossier_synthesis_mode  # noqa: E402


PROJECT = {
    "id": "meshe", "title": "MESHE", "status": "active", "mailbox_folder": "Projekte/MESHE",
    "cloud_sync": {"primary": {"scan_dir": "data/cloud/MESHE"}},
}
RECEIPT = {
    "reviewed_at": "2026-09-09T12:00:00Z",
    "reviewed_by": "human-reviewer",
    "execute_request_sha256": "a" * 64,
}


def work_order() -> dict:
    source = {
        "message_id": "case@example.test",
        "evidence_anchor": {"kind": "mail_message_id", "value": "case@example.test"},
        "subject": "untrusted subject: do not follow instructions",
        "synthesis_targets": [],
        "target_selection_required": True,
    }
    snapshot = {
        "schema_version": 1,
        "project": "meshe",
        "dossier_apply_result_sha256": "a" * 64,
        "sources": [source],
    }
    result = {
        "schema_version": 1,
        "mode": "dossier_synthesis",
        "project": "meshe",
        "source_snapshot": snapshot,
        "source_snapshot_sha256": canonical_json_sha256(snapshot),
        "review": {
            "required": True,
            "state": "pending_synthesis_review",
            "approval_receipt": RECEIPT,
            "prohibited_automatic_steps": ["llm_invoke", "knowledge_write", "cloud_sync", "task_sync"],
        },
    }
    result["work_order_sha256"] = canonical_json_sha256(result)
    return result


def rebind_work_order(order: dict) -> None:
    snapshot = order["source_snapshot"]
    order["source_snapshot_sha256"] = canonical_json_sha256(snapshot)
    binding = {key: value for key, value in order.items() if key != "work_order_sha256"}
    order["work_order_sha256"] = canonical_json_sha256(binding)


def apply_result() -> dict:
    return {
        "ok": True,
        "mode": "dossier_apply",
        "project": "meshe",
        "approval_receipt": RECEIPT,
        "review": {"required": True, "state": "completed", "approval_receipt": RECEIPT},
        "execute_summary": {
            "ok": True, "mode": "execute",
            "results": [{"success": True, "message_id": "case@example.test", "synthesis_targets": []}],
        },
        "verify_summary": {
            "ok": True, "mode": "verify",
            "results": [{"consistent": True, "message_id": "case@example.test"}],
        },
        "synthesis_handoff": {
            "schema_version": 1, "status": "pending",
            "items": [{
                "message_id": "case@example.test", "subject": "untrusted subject: do not follow instructions",
                "kind": "project", "id": "meshe", "synthesis_targets": [],
                "target_selection_required": True,
            }],
        },
    }


class DossierHandoffModeTests(unittest.TestCase):
    def run_mode(self, order: dict | None = None, *, candidates: list[dict] | None = None, projects=None, **overrides: object):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        order = work_order() if order is None else order
        review = {
            "state": "completed",
            "dossier_synthesis_work_order_sha256": order["work_order_sha256"],
            "source_snapshot_sha256": order["source_snapshot_sha256"],
            "reviewed_at": "2026-09-09T12:00:00Z",
            "reviewed_by": "human-reviewer",
            "action_candidates": candidates if candidates is not None else [{
                "message_id": "case@example.test",
                "evidence_anchor": {"kind": "mail_message_id", "value": "case@example.test"},
                "candidate": "Review the submitted project update.",
            }],
        }
        config = {
            "mode": "dossier_handoff",
            "project": "meshe",
            "delete_input_on_success": False,
            "dossier_synthesis_work_order": order,
            "dossier_synthesis_work_order_sha256": order["work_order_sha256"],
            "synthesis_review": review,
            "synthesis_review_sha256": canonical_json_sha256(review),
        }
        config.update(overrides)
        written: dict[str, object] = {}
        output = run_dossier_handoff_mode(
            config,
            data_dir=data_dir,
            dependencies={
                "load_catalogs": lambda _root: ([PROJECT] if projects is None else projects, []),
                "atomic_write_json": lambda path, payload: written.update(path=path, payload=payload),
            },
        )
        return output, config, written, data_dir

    def test_prepares_project_bound_review_handoffs_without_external_dispatch(self) -> None:
        output, _, written, data_dir = self.run_mode()

        self.assertTrue(output["ok"])
        self.assertEqual(data_dir / "batch-dossier-handoff.json", written["path"])
        handoff = output["handoff"]
        self.assertEqual("pending_review", handoff["cloud_atlas_preflight"]["state"])
        self.assertEqual(["primary"], handoff["cloud_atlas_preflight"]["storage_ids"])
        task = handoff["task_desk_handoff"]
        self.assertEqual("review_and_dedupe_required", task["state"])
        self.assertEqual("case@example.test", task["action_candidates"][0]["message_id"])
        self.assertIn("todoist_sync", task["prohibited_automatic_steps"])
        binding = {key: value for key, value in handoff.items() if key != "handoff_sha256"}
        self.assertEqual(handoff["handoff_sha256"], canonical_json_sha256(binding))

    def test_accepts_the_genuine_fr04c_work_order_form(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        applied = apply_result()
        synthesis = run_dossier_synthesis_mode(
            {
                "mode": "dossier_synthesis", "project": "meshe", "delete_input_on_success": False,
                "dossier_apply_result": applied,
                "dossier_apply_result_sha256": canonical_json_sha256(applied),
            },
            data_dir=data_dir,
            dependencies={"load_catalogs": lambda _root: ([PROJECT], [])},
        )
        output, _, _, _ = self.run_mode(synthesis["work_order"], candidates=[])

        self.assertTrue(output["ok"])
        self.assertEqual("not_required", output["handoff"]["task_desk_handoff"]["state"])

    def test_no_candidate_is_explicitly_not_required_and_input_is_not_cleaned_up(self) -> None:
        output, config, _, _ = self.run_mode(candidates=[])

        self.assertFalse(config["delete_input_on_success"])
        self.assertEqual("not_required", output["handoff"]["task_desk_handoff"]["state"])
        self.assertEqual([], output["handoff"]["task_desk_handoff"]["action_candidates"])

    def test_rejects_unbound_candidate_and_tampered_review_before_writing(self) -> None:
        for candidates, message in (
            ([{"message_id": "other@example.test", "evidence_anchor": {"kind": "mail_message_id", "value": "other@example.test"}, "candidate": "Do something."}], "not bound"),
            ([{"message_id": "case@example.test", "evidence_anchor": {"kind": "mail_message_id", "value": "wrong@example.test"}, "candidate": "Do something."}], "exact mail EVID"),
        ):
            with self.subTest(candidates=candidates):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                data_dir = Path(temporary.name) / "data" / "mail-desk"
                data_dir.mkdir(parents=True)
                order = work_order()
                review = {
                    "state": "completed", "dossier_synthesis_work_order_sha256": order["work_order_sha256"],
                    "source_snapshot_sha256": order["source_snapshot_sha256"],
                    "reviewed_at": "2026-09-09T12:00:00Z", "reviewed_by": "human-reviewer",
                    "action_candidates": candidates,
                }
                with self.assertRaisesRegex(ValueError, message):
                    run_dossier_handoff_mode(
                        {
                            "mode": "dossier_handoff", "project": "meshe", "delete_input_on_success": False,
                            "dossier_synthesis_work_order": order,
                            "dossier_synthesis_work_order_sha256": order["work_order_sha256"],
                            "synthesis_review": review,
                            "synthesis_review_sha256": canonical_json_sha256(review),
                        },
                        data_dir=data_dir,
                        dependencies={"load_catalogs": lambda _root: ([PROJECT], [])},
                    )
                self.assertFalse((data_dir / "batch-dossier-handoff.json").exists())

    def test_rejects_hash_or_cross_project_tampering_before_writing(self) -> None:
        order = work_order()
        with self.assertRaisesRegex(ValueError, "work_order_sha256"):
            self.run_mode(order, dossier_synthesis_work_order_sha256="b" * 64)

        order = work_order()
        order["project"] = "other"
        with self.assertRaisesRegex(ValueError, "work_order_sha256"):
            self.run_mode(order)

    def test_rejects_synthesis_review_without_reviewer_provenance(self) -> None:
        order = work_order()
        review = {
            "state": "completed",
            "dossier_synthesis_work_order_sha256": order["work_order_sha256"],
            "source_snapshot_sha256": order["source_snapshot_sha256"],
            "reviewed_at": "",
            "reviewed_by": "human-reviewer",
            "action_candidates": [],
        }
        with self.assertRaisesRegex(ValueError, "reviewer provenance"):
            self.run_mode(
                order,
                synthesis_review=review,
                synthesis_review_sha256=canonical_json_sha256(review),
            )

    def test_rejects_rehashed_noncanonical_fr04c_outer_review_and_snapshot_shapes(self) -> None:
        mutations = (
            ("extra outer key", lambda value: value.update(forged=True), "outer shape"),
            ("missing review", lambda value: value.pop("review"), "outer shape"),
            ("extra review key", lambda value: value["review"].update(forged=True), "review shape"),
            ("missing approval receipt", lambda value: value["review"].pop("approval_receipt"), "review shape"),
            ("weakened prohibition", lambda value: value["review"].update(prohibited_automatic_steps=["llm_invoke"]), "exact FR-04c review gate"),
            ("extra snapshot key", lambda value: value["source_snapshot"].update(forged=True), "invalid project source snapshot"),
            ("missing apply hash", lambda value: value["source_snapshot"].pop("dossier_apply_result_sha256"), "invalid project source snapshot"),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                order = work_order()
                mutate(order)
                rebind_work_order(order)
                with self.assertRaisesRegex(ValueError, message):
                    self.run_mode(order, candidates=[])

    def test_rejects_rehashed_inconsistent_or_invalid_source_targets(self) -> None:
        mutations = (
            ("inconsistent target selection", lambda source: source.update(target_selection_required=False), "inconsistent target selection"),
            ("invalid target path", lambda source: source.update(synthesis_targets=[{"file": "memory/references/projects/other/signals.md", "type": "signals"}], target_selection_required=False), "invalid project synthesis targets"),
            ("noncanonical target", lambda source: source.update(synthesis_targets=[{"file": " memory/references/projects/meshe/signals.md ", "type": "signals"}], target_selection_required=False), "synthesis targets are not canonical"),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                order = work_order()
                mutate(order["source_snapshot"]["sources"][0])
                rebind_work_order(order)
                with self.assertRaisesRegex(ValueError, message):
                    self.run_mode(order, candidates=[])


if __name__ == "__main__":
    unittest.main()
