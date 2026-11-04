"""Context-consultation metadata contract (Issue #64).

Hermetic tests for ``core.context_consultation``: the additive, read-only pre-scan
that attaches bounded ``decision.artifact_candidates.events`` and ``.cloud_storage``
hints to classified draft/inspect items.  No mailbox, network or consumer data.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import mail_desk_batch_runner as runner  # noqa: E402
from core import context_consultation  # noqa: E402
from core.context_consultation import add_context_consultation_metadata  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
from core.modes import inspect as inspect_mode  # noqa: E402

TOPIC_EVENT_PATH = (
    "memory/references/topics/operations/subtopics/course-design/events/{slug}/index.md"
)


def _write(path: Path, text: str = "# Dossier\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project_dossier(workspace: Path, project: str, slug: str, text: str = "# Dossier\n") -> None:
    _write(workspace / "memory" / "references" / "projects" / project / "events" / slug / "index.md", text)


def _topic_event(slug: str, **changes: object) -> dict:
    value = {
        "id": slug,
        "title": slug.replace("-", " ").title(),
        "starts_on": "2026-05-04",
        "reference_md": TOPIC_EVENT_PATH.format(slug=slug),
        "status": "active",
    }
    value.update(changes)
    return value


def _topic(events: list[dict], **changes: object) -> dict:
    value = {
        "id": "operations",
        "title": "Operations",
        "cloud_sync": {"topic-store": {"scan_dir": "data/cloud/TOPIC"}},
        "subtopics": [
            {
                "id": "course-design",
                "title": "Course Design",
                "cloud_sync": {"sub-store": {"scan_dir": "data/cloud/SUB"}},
                "events": events,
            }
        ],
    }
    value.update(changes)
    return value


def _item(kind: str, identifier: str, **decision: object) -> dict:
    return {
        "envelope_id": "1",
        "message_id": "m1@example.test",
        "subject": "Workshop planning",
        "action": {"type": "copy_as_move", "target_folder": "Projects/X"},
        "decision": {"kind": kind, "id": identifier, "confidence": "high", "needs_reply": False, **decision},
        "notes": "n",
        "evidence": {"file": "memory/evidence/x/2026-11.md", "entry": "e"},
        "synthesis_targets": [],
    }


class ProjectMetadataTests(unittest.TestCase):
    def test_project_events_and_cloud_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "pilot", "kickoff-day", "# Kickoff Day\n\nBody\n")
            projects = [{"id": "pilot", "cloud_sync": {"pilot-store": {"scan_dir": "x"}}}]
            items = [_item("project", "pilot")]
            add_context_consultation_metadata(items, workspace_root=workspace, projects=projects, topics=[])

        candidates = items[0]["decision"]["artifact_candidates"]
        self.assertEqual(
            [
                {
                    "id": "kickoff-day",
                    "title": "Kickoff Day",
                    "reference_md": "memory/references/projects/pilot/events/kickoff-day/index.md",
                    "reasons": ["existing_event_dossier"],
                }
            ],
            candidates["events"],
        )
        self.assertEqual(
            [{"scope": "project", "storage_id": "pilot-store", "reasons": ["declared_cloud_sync"]}],
            candidates["cloud_storage"],
        )
        serialized = json.dumps(candidates)
        self.assertNotIn(temporary.replace("\\", "/"), serialized.replace("\\\\", "/"))
        self.assertNotIn("\\", serialized)

    def test_project_loaded_from_catalog_files_when_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            projects_dir = workspace / "memory" / "references" / "projects"
            _write(projects_dir / "projects.json", json.dumps({"projects": [{"id": "pilot", "cloud_sync": {"s1": {}}}]}))
            items = [_item("project", "pilot")]
            add_context_consultation_metadata(items, workspace_root=workspace)

        self.assertEqual("s1", items[0]["decision"]["artifact_candidates"]["cloud_storage"][0]["storage_id"])

    def test_project_event_title_is_optional_and_slug_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "pilot", "plain", "no heading\n")
            _project_dossier(workspace, "pilot", "Bad_Slug")
            _project_dossier(workspace, "pilot", "-leading")
            _write(workspace / "memory" / "references" / "projects" / "pilot" / "events" / "no-index" / "notes.md")
            items = [_item("project", "pilot")]
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[{"id": "pilot"}], topics=[]
            )

        events = items[0]["decision"]["artifact_candidates"]["events"]
        self.assertEqual(["plain"], [event["id"] for event in events])
        self.assertNotIn("title", events[0])

    def test_symlinked_event_directory_is_not_consulted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace = base / "workspace"
            outside = base / "outside"
            _project_dossier(outside, "pilot", "linked", "# Linked\n")
            events_dir = workspace / "memory" / "references" / "projects" / "pilot" / "events"
            events_dir.mkdir(parents=True)
            try:
                os.symlink(outside / "memory" / "references" / "projects" / "pilot" / "events" / "linked", events_dir / "linked", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are not available on this platform")
            items = [_item("project", "pilot")]
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[{"id": "pilot"}], topics=[]
            )
        self.assertNotIn("artifact_candidates", items[0]["decision"])

    def test_case_insensitive_unique_catalog_id_is_accepted_but_ambiguous_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "Pilot", "kickoff")
            items = [_item("project", "pilot")]
            add_context_consultation_metadata(items, workspace_root=workspace, projects=[{"id": "Pilot"}], topics=[])
            self.assertEqual(
                "memory/references/projects/Pilot/events/kickoff/index.md",
                items[0]["decision"]["artifact_candidates"]["events"][0]["reference_md"],
            )
            ambiguous = [_item("project", "pilot")]
            add_context_consultation_metadata(
                ambiguous,
                workspace_root=workspace,
                projects=[{"id": "Pilot", "cloud_sync": {"a": {}}}, {"id": "PILOT", "cloud_sync": {"b": {}}}],
                topics=[],
            )
            self.assertNotIn("artifact_candidates", ambiguous[0]["decision"])


class TopicMetadataTests(unittest.TestCase):
    def test_topic_subtopic_event_and_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = _topic_event("spring-forum-2026", title="Spring Forum 2026", aliases=["Spring Forum"])
            _write(workspace / event["reference_md"])
            items = [_item("topic", "operations", subtopic="course-design")]
            items[0]["subject"] = "Question about Spring Forum 2026"
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[], topics=[_topic([event])]
            )

        candidates = items[0]["decision"]["artifact_candidates"]
        self.assertEqual(1, len(candidates["events"]))
        row = candidates["events"][0]
        self.assertEqual("spring-forum-2026", row["id"])
        self.assertEqual("Spring Forum 2026", row["title"])
        self.assertEqual("course-design", row["subtopic"])
        self.assertEqual(event["reference_md"], row["reference_md"])
        self.assertTrue(any(reason.startswith("subject_id:") or reason.startswith("subject_title:") for reason in row["reasons"]))
        self.assertEqual(
            [
                {"scope": "subtopic", "storage_id": "sub-store", "reasons": ["declared_cloud_sync"]},
                {"scope": "topic", "storage_id": "topic-store", "reasons": ["declared_cloud_sync"]},
            ],
            candidates["cloud_storage"],
        )

    def test_topic_without_decided_subtopic_gets_topic_cloud_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = _topic_event("spring-forum-2026")
            _write(workspace / event["reference_md"])
            items = [_item("topic", "operations")]
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[], topics=[_topic([event])]
            )

        candidates = items[0]["decision"]["artifact_candidates"]
        self.assertNotIn("events", candidates)
        self.assertEqual(
            [{"scope": "topic", "storage_id": "topic-store", "reasons": ["declared_cloud_sync"]}],
            candidates["cloud_storage"],
        )

    def test_text_match_is_preferred_and_remaining_slots_are_filled_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            slugs = ["alpha-day", "beta-day", "gamma-day", "delta-day", "zeta-summit"]
            events = [_topic_event(slug) for slug in slugs]
            for event in events:
                _write(workspace / event["reference_md"])
            items = [_item("topic", "operations", subtopic="course-design")]
            items[0]["subject"] = "Re: Zeta Summit logistics"
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[], topics=[_topic(events, cloud_sync={})]
            )

        rows = items[0]["decision"]["artifact_candidates"]["events"]
        self.assertEqual(["zeta-summit", "alpha-day", "beta-day"], [row["id"] for row in rows])

    def test_classifier_selected_and_candidate_events_are_prioritized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            events = [_topic_event(slug) for slug in ("alpha-day", "beta-day", "gamma-day", "zeta-summit")]
            for event in events:
                _write(workspace / event["reference_md"])
            selected_item = _item("topic", "operations", subtopic="course-design", event="zeta-summit")
            candidate_item = _item(
                "topic", "operations", subtopic="course-design", event_candidates=[{"id": "gamma-day"}]
            )
            add_context_consultation_metadata(
                [selected_item, candidate_item], workspace_root=workspace, projects=[], topics=[_topic(events)]
            )

        selected_rows = selected_item["decision"]["artifact_candidates"]["events"]
        self.assertEqual("zeta-summit", selected_rows[0]["id"])
        self.assertIn("selected_event", selected_rows[0]["reasons"])
        candidate_rows = candidate_item["decision"]["artifact_candidates"]["events"]
        self.assertEqual("gamma-day", candidate_rows[0]["id"])
        self.assertIn("event_candidate", candidate_rows[0]["reasons"])

    def test_preview_text_participates_in_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            events = [_topic_event(slug, title=slug) for slug in ("a-event", "b-event", "c-event", "target-event")]
            for event in events:
                _write(workspace / event["reference_md"])
            items = [_item("topic", "operations", subtopic="course-design")]
            items[0]["preview"] = "please check target event details"
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[], topics=[_topic(events, cloud_sync={})]
            )

        rows = items[0]["decision"]["artifact_candidates"]["events"]
        self.assertEqual("target-event", rows[0]["id"])

    def test_missing_dossier_noncanonical_reference_and_inactive_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            good = _topic_event("good-one")
            missing = _topic_event("missing-one")
            wrong_ref = _topic_event("wrong-ref", reference_md="memory/references/topics/other/x/index.md")
            backslash = _topic_event("back-slash", reference_md=TOPIC_EVENT_PATH.format(slug="back-slash").replace("/", "\\"))
            inactive = _topic_event("inactive-one", status="inactive")
            bad_id = _topic_event("Bad_ID", reference_md=TOPIC_EVENT_PATH.format(slug="Bad_ID"))
            no_title = _topic_event("no-title", title="")
            for event in (good, wrong_ref, inactive, bad_id, no_title):
                _write(workspace / event["reference_md"])
            _write(workspace / "memory/references/topics/other/x/index.md")
            items = [_item("topic", "operations", subtopic="course-design")]
            add_context_consultation_metadata(
                items,
                workspace_root=workspace,
                projects=[],
                topics=[_topic([good, missing, wrong_ref, backslash, inactive, bad_id, no_title], cloud_sync={})],
            )

        rows = items[0]["decision"]["artifact_candidates"]["events"]
        self.assertEqual(["good-one"], [row["id"] for row in rows])

    def test_duplicate_event_ids_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first = _topic_event("same-id")
            second = _topic_event("same-id", title="Other Title")
            _write(workspace / first["reference_md"])
            items = [_item("topic", "operations", subtopic="course-design")]
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[], topics=[_topic([first, second], cloud_sync={})]
            )

        self.assertNotIn("events", items[0]["decision"].get("artifact_candidates", {}))


class BoundsAndPreservationTests(unittest.TestCase):
    def test_no_candidates_means_no_artifact_candidates_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            items = [_item("project", "pilot"), _item("unknown", "unclassified"), _item("notification", "x")]
            add_context_consultation_metadata(
                items, workspace_root=Path(temporary), projects=[{"id": "pilot"}], topics=[]
            )
        for item in items:
            self.assertNotIn("artifact_candidates", item["decision"])

    def test_existing_artifact_candidates_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "pilot", "kickoff")
            existing = {"workpackage": [{"id": "WP1", "title": "One"}], "task": [{"id": "T1", "title": "t"}]}
            items = [_item("project", "pilot", artifact_candidates=copy.deepcopy(existing))]
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[{"id": "pilot", "cloud_sync": {"s": {}}}], topics=[]
            )

        candidates = items[0]["decision"]["artifact_candidates"]
        self.assertEqual(existing["workpackage"], candidates["workpackage"])
        self.assertEqual(existing["task"], candidates["task"])
        self.assertIn("events", candidates)
        self.assertIn("cloud_storage", candidates)

    def test_existing_events_key_is_not_overwritten_and_non_dict_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "pilot", "kickoff")
            keep = [{"id": "pre-existing"}]
            items = [
                _item("project", "pilot", artifact_candidates={"events": keep}),
                _item("project", "pilot", artifact_candidates="opaque"),
            ]
            add_context_consultation_metadata(
                items, workspace_root=workspace, projects=[{"id": "pilot"}], topics=[]
            )

        self.assertEqual(keep, items[0]["decision"]["artifact_candidates"]["events"])
        self.assertEqual("opaque", items[1]["decision"]["artifact_candidates"])

    def test_bounded_to_three_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for slug in ("e5", "e2", "e4", "e1", "e3"):
                _project_dossier(workspace, "pilot", slug)
            storages = {key: {} for key in ("s5", "s2", "s4", "s1", "s3")}
            project = {"id": "pilot", "cloud_sync": storages}
            first = [_item("project", "pilot")]
            second = [_item("project", "pilot")]
            add_context_consultation_metadata(first, workspace_root=workspace, projects=[project], topics=[])
            add_context_consultation_metadata(
                second, workspace_root=workspace, projects=[dict(project, cloud_sync=dict(reversed(list(storages.items()))))], topics=[]
            )

        candidates = first[0]["decision"]["artifact_candidates"]
        self.assertEqual(["e1", "e2", "e3"], [row["id"] for row in candidates["events"]])
        self.assertEqual(["s1", "s2", "s3"], [row["storage_id"] for row in candidates["cloud_storage"]])
        self.assertEqual(first[0]["decision"], second[0]["decision"])

    def test_cloud_storage_ids_must_be_plain_nonempty_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = {
                "id": "pilot",
                "cloud_sync": {
                    "": {},
                    "  ": {},
                    "data/cloud/path": {},
                    "C:\\drive": {},
                    "https://example.test/x": {},
                    "..": {},
                    "with space": {},
                    "ok-store": {},
                },
            }
            items = [_item("project", "pilot")]
            add_context_consultation_metadata(items, workspace_root=Path(temporary), projects=[project], topics=[])

        rows = items[0]["decision"]["artifact_candidates"]["cloud_storage"]
        self.assertEqual(["ok-store"], [row["storage_id"] for row in rows])

    def test_only_artifact_candidates_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "pilot", "kickoff")
            event = _topic_event("spring-forum-2026")
            _write(workspace / event["reference_md"])
            items = [
                _item("project", "pilot"),
                _item("topic", "operations", subtopic="course-design"),
                _item("unknown", "unclassified"),
            ]
            before = copy.deepcopy(items)
            add_context_consultation_metadata(
                items,
                workspace_root=workspace,
                projects=[{"id": "pilot", "cloud_sync": {"s": {}}}],
                topics=[_topic([event])],
            )

        for old, new in zip(before, items):
            new_decision = dict(new["decision"])
            new_decision.pop("artifact_candidates", None)
            self.assertEqual(old["decision"], new_decision)
            for key in old:
                if key != "decision":
                    self.assertEqual(old[key], new[key], key)
            self.assertEqual(set(old), set(new))
        self.assertIn("artifact_candidates", items[0]["decision"])
        self.assertIn("artifact_candidates", items[1]["decision"])
        self.assertNotIn("artifact_candidates", items[2]["decision"])

    def test_read_only_no_files_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _project_dossier(workspace, "pilot", "kickoff")
            snapshot = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*"))
            add_context_consultation_metadata(
                [_item("project", "pilot")], workspace_root=workspace, projects=[{"id": "pilot"}], topics=[]
            )
            after = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*"))
        self.assertEqual(snapshot, after)

    def test_malformed_items_and_catalogs_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            items = [None, "x", {}, {"decision": "no"}, {"decision": {"kind": "project", "id": 5}}]
            add_context_consultation_metadata(
                items,  # type: ignore[arg-type]
                workspace_root=Path(temporary),
                projects=[None, {"id": ["x"]}, {"id": "pilot", "cloud_sync": "nope"}],  # type: ignore[list-item]
                topics=["bad"],  # type: ignore[list-item]
            )
        self.assertEqual(5, len(items))


class IntegrationSeamTests(unittest.TestCase):
    def test_module_exposes_public_function(self) -> None:
        self.assertTrue(callable(context_consultation.add_context_consultation_metadata))

    def test_draft_and_inspect_call_enrichment_after_classification(self) -> None:
        class _Tracker:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def step(self, *_a: object, **_k: object) -> None:
                pass

            def complete(self, *_a: object, **_k: object) -> None:
                pass

        email = {
            "envelope_id": "1",
            "folder": "INBOX",
            "message_id": "one@example.test",
            "subject": "Hello",
            "from": "a@example.test",
            "to": "b@example.test",
            "date": "2026-11-01",
            "preview": "hi",
        }
        classified = {"items": [_item("project", "pilot")]}

        def fake_draft(emails_arg, *, source_sink=None, **_kwargs):
            return classified

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            consult = Mock()
            draft_result = draft_mode.run_draft_mode(
                {"count": 1, "output_file": str(data_dir / "batch-manifest.json"), "evaluate_attachments": False},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _Tracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": fake_draft,
                    "get_unprocessed_emails": Mock(return_value=([dict(email)], 0)),
                    "load_sent_index": Mock(return_value={}),
                    "add_context_consultation_metadata": consult,
                },
            )
            self.assertTrue(draft_result["ok"])
            consult.assert_called_once()
            args, kwargs = consult.call_args
            self.assertIs(classified["items"], args[0])
            self.assertEqual(Path(temporary), Path(kwargs["workspace_root"]))

            inspect_consult = Mock()
            inspect_result = inspect_mode.run_inspect_mode(
                {"envelope_ids": ["1"], "propose_manifest": True, "output_file": str(data_dir / "ins.json")},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": fake_draft,
                    "get_oldest_envelopes": Mock(),
                    "get_single_email_details": Mock(side_effect=lambda *_a, **_k: dict(email)),
                    "get_unprocessed_emails": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "resolve_data_dir": Mock(return_value=data_dir),
                    "resolve_final_index_path": Mock(return_value=data_dir / "index.json"),
                    "run_himalaya": Mock(),
                    "sleep": Mock(),
                    "add_context_consultation_metadata": inspect_consult,
                },
            )
            self.assertTrue(inspect_result["ok"])
            inspect_consult.assert_called_once()
            self.assertEqual(Path(temporary), Path(inspect_consult.call_args.kwargs["workspace_root"]))

    def test_draft_writes_real_consultation_metadata_into_the_manifest(self) -> None:
        class _Tracker:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def step(self, *_a: object, **_k: object) -> None:
                pass

            def complete(self, *_a: object, **_k: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            _write(
                workspace / "memory" / "references" / "projects" / "projects.json",
                json.dumps({"projects": [{"id": "pilot", "cloud_sync": {"pilot-store": {"scan_dir": "x"}}}]}),
            )
            _project_dossier(workspace, "pilot", "kickoff", "# Kickoff Day\n")
            data_dir = workspace / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            manifest_file = data_dir / "batch-manifest.json"
            email = {
                "envelope_id": "1",
                "folder": "INBOX",
                "message_id": "one@example.test",
                "subject": "Kickoff Day planning",
                "from": "a@example.test",
                "to": "b@example.test",
                "date": "2026-11-01",
                "preview": "hi",
            }
            classified = {"items": [_item("project", "pilot")]}
            result = draft_mode.run_draft_mode(
                {"count": 1, "output_file": str(manifest_file), "evaluate_attachments": False},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "BatchProgressTracker": _Tracker,
                    "atomic_write_json": runner.atomic_write_json,
                    "draft_manifest": lambda _emails, **_kwargs: classified,
                    "get_unprocessed_emails": Mock(return_value=([dict(email)], 0)),
                    "load_sent_index": Mock(return_value={}),
                },
            )

            self.assertTrue(result["ok"])
            written = json.loads(manifest_file.read_text(encoding="utf-8"))
            candidates = written["items"][0]["decision"]["artifact_candidates"]
            self.assertEqual(["kickoff"], [row["id"] for row in candidates["events"]])
            self.assertEqual("pilot-store", candidates["cloud_storage"][0]["storage_id"])
            self.assertEqual(1, result["candidate_count"])

    def test_inspect_without_proposal_does_not_consult(self) -> None:
        email = {
            "envelope_id": "1", "folder": "INBOX", "message_id": "one@example.test",
            "subject": "Hello", "from": "a@example.test", "to": "b@example.test", "date": "2026-11-01",
        }
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            consult = Mock()
            inspect_mode.run_inspect_mode(
                {"envelope_ids": ["1"]},
                account="primary",
                data_dir=data_dir,
                dependencies={
                    "get_oldest_envelopes": Mock(),
                    "get_single_email_details": Mock(side_effect=lambda *_a, **_k: dict(email)),
                    "get_unprocessed_emails": Mock(),
                    "load_final_index": Mock(return_value={"items": {}}),
                    "resolve_data_dir": Mock(return_value=data_dir),
                    "resolve_final_index_path": Mock(return_value=data_dir / "index.json"),
                    "run_himalaya": Mock(),
                    "sleep": Mock(),
                    "add_context_consultation_metadata": consult,
                },
            )
        consult.assert_not_called()


if __name__ == "__main__":
    unittest.main()
