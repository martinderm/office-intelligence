"""FR-06b contracts for review-enriched synthesis targets."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core.modes import execute as execute_mode  # noqa: E402
from core.modes import pipeline as pipeline_mode  # noqa: E402


class SynthesisTargetsTests(unittest.TestCase):
    @staticmethod
    def item(
        envelope_id: str,
        *,
        kind: str = "project",
        identifier: str = "meshe",
        targets: object = None,
        action: dict[str, str] | None = None,
    ) -> dict:
        item = {
            "envelope_id": envelope_id,
            "message_id": f"{envelope_id}@example.test",
            "subject": f"Subject {envelope_id}",
            "action": action or {"type": "keep_in_folder"},
            "decision": {"kind": kind, "id": identifier},
        }
        if targets is not None:
            item["synthesis_targets"] = targets
        return item

    @staticmethod
    def target(path: str, **extra: str) -> dict[str, str]:
        return {"file": path, "type": "statusampel", **extra}

    @staticmethod
    def dependencies(**overrides: Mock) -> dict:
        defaults = {
            "BatchProgressTracker": Mock(),
            "append_action_log_entry": Mock(),
            "auto_resolve_replies_from_sent": Mock(),
            "flush_batch_evidence": Mock(),
            "load_final_index": Mock(return_value={"items": {}}),
            "run_himalaya": Mock(),
            "save_final_index_atomic": Mock(),
            "sleep": Mock(),
            "verify_in_target_folder": Mock(return_value="copied"),
        }
        defaults.update(overrides)
        return defaults

    def test_classifier_defaults_targets_and_does_not_forward_untrusted_mail_field(self) -> None:
        untrusted_targets = [
            self.target("memory/references/projects/meshe/statusampel.md")
        ]
        item = classifier.classify_email(
            {
                "envelope_id": "1",
                "message_id": "source@example.test",
                "subject": "Unclassified",
                "synthesis_targets": untrusted_targets,
            },
            projects=[],
            topics=[],
            sent_lookup={},
            final_index={"items": {}},
        )

        self.assertEqual([], item["synthesis_targets"])
        self.assertIsNot(item["synthesis_targets"], untrusted_targets)

    def test_draft_manifest_defaults_targets_for_untrusted_inspected_input(self) -> None:
        untrusted_targets = [
            self.target("memory/references/projects/meshe/statusampel.md")
        ]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = classifier.draft_manifest(
                [
                    {
                        "envelope_id": "draft-source",
                        "message_id": "draft-source@example.test",
                        "subject": "Unclassified",
                        "synthesis_targets": untrusted_targets,
                    }
                ],
                workspace_root=Path(temporary),
                sent_lookup={},
                final_index={"items": {}},
            )

        self.assertEqual([], manifest["items"][0]["synthesis_targets"])

    def test_successful_project_and_topic_results_round_trip_targets_and_keep_telemetry_exact(self) -> None:
        project_target = self.target(
            "memory/references/projects/meshe/statusampel.md",
            recommended_action="review_update",
            task_anchor="WP2",
        )
        topic_target = self.target(
            "memory/references/topics/dienstreisen/subtopics/cagliari.md",
            section="Logistik",
        )
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = execute_mode.run_execute_mode(
                {
                    "items": [
                        self.item("project", targets=[project_target]),
                        self.item(
                            "topic",
                            kind="topic",
                            identifier="dienstreisen",
                            targets=[topic_target],
                        ),
                    ]
                },
                data_dir=data_dir,
                index_path=data_dir / "index.json",
                dependencies=self.dependencies(),
            )

        self.assertTrue(result["ok"])
        self.assertEqual([[project_target], [topic_target]], [
            row["synthesis_targets"] for row in result["results"]
        ])
        self.assertEqual(
            {
                "affected_projects": ["meshe"],
                "affected_topics": ["dienstreisen"],
                "synthesis_required": True,
            },
            result["telemetry"],
        )
        self.assertEqual(
            {"affected_projects", "affected_topics", "synthesis_required"},
            set(result["telemetry"]),
        )

    def test_failed_item_has_no_synthesis_targets(self) -> None:
        good_target = self.target("memory/references/projects/meshe/statusampel.md")
        failed_target = self.target("memory/references/topics/dienstreisen/signals.md")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = execute_mode.run_execute_mode(
                {
                    "items": [
                        self.item("good", targets=[good_target]),
                        self.item(
                            "failed",
                            kind="topic",
                            identifier="dienstreisen",
                            targets=[failed_target],
                            action={"type": "copy_as_move", "target_folder": "Topics/Travel"},
                        ),
                    ]
                },
                data_dir=data_dir,
                index_path=data_dir / "index.json",
                dependencies=self.dependencies(run_himalaya=Mock(side_effect=RuntimeError("copy failed"))),
            )

        self.assertFalse(result["ok"])
        self.assertEqual([good_target], result["results"][0]["synthesis_targets"])
        self.assertEqual([], result["results"][1]["synthesis_targets"])

    def test_invalid_targets_abort_the_whole_batch_before_any_mutation(self) -> None:
        valid = self.item(
            "valid-first",
            targets=[self.target("memory/references/projects/meshe/statusampel.md")],
        )
        invalid_cases = {
            "form": self.item("invalid", targets=["not-an-object"]),
            "path": self.item(
                "invalid",
                targets=[self.target("memory/references/projects/meshe/../outside.md")],
            ),
            "decision-root": self.item(
                "invalid",
                targets=[self.target("memory/references/topics/meshe/signals.md")],
            ),
            "decision-slug": self.item(
                "invalid",
                identifier="meshe/other",
                targets=[self.target("memory/references/projects/meshe/other/status.md")],
            ),
            "type-label": self.item(
                "invalid",
                targets=[self.target("memory/references/projects/meshe/statusampel.md", type="needs review")],
            ),
            "ads-path": self.item(
                "invalid",
                targets=[self.target("memory/references/projects/meshe/signals.md:stream.md")],
            ),
            "trailing-space-segment": self.item(
                "invalid",
                targets=[self.target("memory/references/projects/meshe/notes. /signals.md")],
            ),
        }

        for name, invalid in invalid_cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary) / "data" / "mail-desk"
                data_dir.mkdir(parents=True)
                dependencies = self.dependencies()
                with self.assertRaisesRegex(ValueError, r"Execute item 1"):
                    execute_mode.run_execute_mode(
                        {"items": [deepcopy(valid), invalid]},
                        data_dir=data_dir,
                        index_path=data_dir / "index.json",
                        dependencies=dependencies,
                    )

                for dependency in dependencies.values():
                    dependency.assert_not_called()

    def test_pipeline_keeps_targets_inside_execute_summary(self) -> None:
        target = self.target("memory/references/projects/meshe/statusampel.md")
        executable = self.item("pipeline", targets=[target])
        executable["decision"]["confidence"] = "high"
        executable["action"]["target_folder"] = "Projects/MESHE"
        execute_result = {
            "ok": True,
            "results": [{"success": True, "synthesis_targets": [target]}],
            "telemetry": {
                "affected_projects": ["meshe"],
                "affected_topics": [],
                "synthesis_required": True,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            result = pipeline_mode.run_pipeline_mode(
                {"verify": False, "sync_sent": False},
                data_dir=data_dir,
                dependencies={
                    "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "pipeline"}], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "draft_manifest": Mock(return_value={"items": [executable]}),
                    "run_execute_mode": Mock(return_value=execute_result),
                },
            )

        self.assertEqual(execute_result, result["execute_summary"])
        self.assertNotIn("synthesis_targets", result)


if __name__ == "__main__":
    unittest.main()
