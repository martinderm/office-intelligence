"""Historical issue #8 pins; external writer boundary tracked separately in #14."""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core.modes.dossier_synthesis import canonical_json_sha256
from core.batch_contract import add_draft_contract, canonical_execute_request_sha256 as batch_hash
from core.modes.dossier_apply import canonical_execute_request_sha256 as dossier_hash


class EventDossierTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module("mail_desk_event_dossier")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_stage(self, stage, **values):
        result = self.api.process_request({"schema_version": 1, "stage": stage, **values}, workspace_root=self.root)
        self.assertIsInstance(result, dict)
        return result

    def scope(self):
        return {"account": "synthetic", "folders": ["INBOX", "Archive"], "terms": ["workshop"],
                "scan_limit_per_folder": 20, "inspect_limit": 5}

    def source(self, folder="INBOX", eid="1", mid="source@example.test", sha="a" * 64):
        return {"search": {"account": "synthetic", "folder": folder, "envelope_id": eid, "message_id": mid},
                "read": {"account": "synthetic", "folder": folder, "envelope_id": eid,
                         "message_id": mid, "content_sha256": "b" * 64, "references": [], "in_reply_to": None},
                "attachments": [{"account": "synthetic", "folder": folder, "envelope_id": eid,
                    "message_id": mid, "filename": "program.pdf", "original_filename": "program.pdf",
                    "mime_type": "application/pdf", "size_bytes": 20, "sha256": sha, "part_locator": "2",
                    "fetch_status": "available", "provenance": "rfc822_mime", "policy_status": "allowed"}],
                "calendar_objects": []}

    def operative(self):
        item = {"envelope_id": "1", "source_folder": "INBOX", "message_id": "source@example.test",
                "subject": "Synthetic workshop", "action": {"type": "copy_as_move", "target_folder": "Projects/demo"},
                "decision": {"kind": "project", "id": "demo", "confidence": "high", "needs_reply": False}}
        execute = add_draft_contract({"mode": "execute", "account": "synthetic", "items": [item]},
            expected_count=1, allow_fewer=False, source_folder="INBOX", account="synthetic", skip_known=False)
        return {"mode": "dossier_apply", "project": "demo", "account": "synthetic",
                "execute_request": execute, "delete_input_on_success": False,
                "review": {"required": True, "state": "pending"}}

    def review(self, sources=None, operative=None):
        return self.run_stage("review", scope=self.scope(), sources=sources or [self.source()],
                              operative_request=operative or self.operative(), attachment_requests=[])

    def approved(self):
        operative = self.operative()
        execute = operative["execute_request"]
        execute["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:00:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": batch_hash(execute)}}
        receipt = {"reviewed_at": "2026-01-01T12:01:00Z", "reviewed_by": "human-fixture",
                   "execute_request_sha256": dossier_hash(execute)}
        operative["review"] = {"required": True, "state": "approved", "approval_receipt": receipt}
        return operative

    def applied(self, operative):
        receipt = operative["review"]["approval_receipt"]
        return {"ok": True, "mode": "dossier_apply", "project": "demo", "approval_receipt": receipt,
            "review": {"required": True, "state": "completed", "approval_receipt": receipt},
            "execute_summary": {"ok": True, "mode": "execute", "results": [{"success": True,
                "message_id": "source@example.test", "synthesis_targets": []}]},
            "verify_summary": {"ok": True, "mode": "verify", "results": [{"consistent": True,
                "message_id": "source@example.test"}]},
            "synthesis_handoff": {"schema_version": 1, "status": "pending", "items": [{
                "message_id": "source@example.test", "subject": "Synthetic workshop", "kind": "project",
                "id": "demo", "synthesis_targets": [], "target_selection_required": True}]}}

    def handoff(self, operative=None, applied=None, **changes):
        operative = operative or self.approved()
        applied = applied or self.applied(operative)
        values = {"scope": self.scope(), "apply_request": operative, "apply_result": applied,
                  "apply_result_sha256": canonical_json_sha256(applied), "sources": [self.source()],
                  "event": {"owner_kind": "project", "owner_id": "demo", "id": "workshop",
                            "title": "Synthetic workshop", "start": "2026-01-01"}}
        return self.run_stage("handoff", **{**values, **changes})

    def test_plan_is_explicit_bounded_and_never_claims_completeness(self):
        result = self.run_stage("plan", scope=self.scope())
        self.assertTrue(result["ok"])
        self.assertEqual(["INBOX", "Archive"], [r["folder"] for r in result["search_requests"]])
        self.assertTrue(all(r["mode"] == "search" for r in result["search_requests"]))
        self.assertFalse(result["completeness"]["exhaustive"])
        self.assertEqual("first_page_envelopes", result["completeness"]["scan_basis"])
        self.assertEqual("pending_workspace_revalidation", result["account_binding"])
        self.assertEqual([], list(self.root.iterdir()))

    def test_plan_rejects_implicit_scope_bool_limits_and_oversized_inspection(self):
        for changes in ({"folders": []}, {"folders": ["INBOX", "INBOX"]}, {"terms": []},
                        {"scan_limit_per_folder": True}, {"inspect_limit": 0}):
            with self.subTest(changes=changes):
                self.assertFalse(self.run_stage("plan", scope={**self.scope(), **changes})["ok"])
        self.assertFalse(self.run_stage("plan", scope=self.scope(), inspect_ids=[str(i) for i in range(6)])["ok"])

    def test_review_dedupes_message_and_attachment_hashes_retaining_locations(self):
        result = self.review([self.source(), self.source("Archive", "9", "<SOURCE@example.test>")])
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(result["sources"]))
        self.assertEqual(2, len(result["sources"][0]["locations"]))
        self.assertEqual(1, len(result["attachments"]))
        self.assertEqual(2, len(result["attachments"][0]["provenance"]))
        self.assertEqual("pending", result["operative_request"]["review"]["state"])
        self.assertNotIn("approval_receipt", result["operative_request"]["review"])

    def test_review_rejects_locator_account_message_and_attachment_drift(self):
        for section, field, value in (("read", "account", "other"), ("read", "message_id", "other@example.test"),
                                     ("read", "envelope_id", "9"), ("attachment", "message_id", "other@example.test"),
                                     ("attachment", "sha256", "invalid")):
            source = self.source()
            target = source["attachments"][0] if section == "attachment" else source[section]
            target[field] = value
            with self.subTest(section=section, field=field):
                self.assertFalse(self.review([source])["ok"])

    def test_calendar_conflicts_and_missing_thread_parents_require_semantic_review(self):
        first, second = self.source(), self.source("Archive", "9")
        first["calendar_objects"] = [{"uid": "calendar-a", "sequence": 1, "recurrence_id": None, "content_sha256": "c" * 64}]
        second["calendar_objects"] = [{"uid": "calendar-b", "sequence": 2, "recurrence_id": None, "content_sha256": "d" * 64}]
        first["read"]["in_reply_to"] = "missing@example.test"
        result = self.review([first, second])
        self.assertTrue(result["review_required"])
        self.assertTrue(result["calendar_conflicts"])
        self.assertEqual(["missing@example.test"], result["missing_thread_parents"])

    def test_same_message_identity_different_content_is_not_silently_merged(self):
        first, second = self.source(), self.source("Archive", "9")
        second["read"]["content_sha256"] = "e" * 64
        self.assertFalse(self.review([first, second])["ok"])

    def test_nested_review_hashes_are_ordered_and_bind_exact_final_inner_receipt(self):
        operative = self.operative()
        initial = self.review(operative=operative)
        self.assertEqual(batch_hash(operative["execute_request"]), initial["review_binding"]["inner_sha256"])
        self.assertEqual(dossier_hash(operative["execute_request"]), initial["review_binding"]["outer_sha256"])
        approved = copy.deepcopy(operative)
        approved["execute_request"]["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:00:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": batch_hash(approved["execute_request"])}}
        frozen = self.review(operative=approved)
        self.assertEqual(initial["review_binding"]["inner_sha256"], frozen["review_binding"]["inner_sha256"])
        self.assertNotEqual(initial["review_binding"]["outer_sha256"], frozen["review_binding"]["outer_sha256"])
        approved["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:01:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": frozen["review_binding"]["outer_sha256"]}}
        self.assertTrue(self.review(operative=approved)["ok"])
        approved["execute_request"]["items"][0]["action"]["target_folder"] = "Other"
        self.assertFalse(self.review(operative=approved)["ok"])

    def test_handoff_rejects_partial_apply_and_report_rejects_missing_artifacts(self):
        self.assertFalse(self.run_stage("handoff", scope=self.scope(), apply_request=self.operative(),
            apply_result={"ok": False, "results": []}, event={"id": "workshop"})["ok"])
        self.assertFalse(self.run_stage("report", artifacts=["memory/references/projects/demo/events/workshop/index.md"])["ok"])

    def test_unknown_fields_and_stages_fail_closed(self):
        self.assertFalse(self.run_stage("plan", scope=self.scope(), execute=True)["ok"])
        self.assertFalse(self.run_stage("execute", scope=self.scope())["ok"])

    def test_repeat_review_is_deterministic_and_never_changes_operative_request(self):
        request = self.operative()
        before = copy.deepcopy(request)
        self.assertEqual(self.review(operative=request), self.review(operative=request))
        self.assertEqual(before, request)
        self.assertEqual([], list(self.root.iterdir()))

    def test_completed_apply_handoff_is_source_bound_and_report_verifies_outputs(self):
        # Historical pin used a nonexistent event writer (tracked as #14).
        # Keep real handoff/report assertions; synthesize only the writer output
        # and its explicit source-bound receipt, not a mock of facade validation.
        operative = self.approved()
        applied = self.applied(operative)
        handoff = self.handoff(operative, applied)
        self.assertTrue(handoff["ok"])
        order = handoff["work_order"]
        self.assertEqual("event-documentation", order["owner"])
        self.assertEqual("source@example.test", order["sources"][0]["message_id"])
        self.assertEqual("pending_content_review", order["status"])
        self.assertFalse(handoff["completeness"]["exhaustive"])
        artifact = "memory/references/projects/demo/events/workshop/index.md"
        target = self.root / artifact
        target.parent.mkdir(parents=True)
        target.write_text("Synthetic workshop\n", encoding="utf-8")
        import hashlib
        receipt = {"schema_version": 1, "work_order_sha256": canonical_json_sha256(order),
                   "source_snapshot_sha256": order["source_snapshot_sha256"],
                   "outputs": [{"path": artifact, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}]}
        report = self.run_stage("report", artifacts=[artifact], work_order=order, output_receipt=receipt)
        self.assertTrue(report["ok"])
        self.assertEqual([artifact], report["verified_outputs"])
        self.assertFalse(report["content_approved"])
        applied["verify_summary"]["results"][0]["message_id"] = "other@example.test"
        self.assertFalse(self.handoff(operative, applied)["ok"])

    def test_cli_invalid_json_and_parser_errors_return_bounded_json(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "mail_desk_event_dossier.py"
        request = self.root / "request.json"
        for data in ('{"stage":"plan","stage":"review"}', '{"schema_version":NaN}', '{}'):
            request.write_text(data, encoding="utf-8")
            run = subprocess.run([sys.executable, "-B", str(script), "--request", str(request),
                "--workspace", str(self.root), "--json"], capture_output=True, text=True)
            self.assertNotEqual(0, run.returncode)
            envelope = json.loads(run.stdout)
            self.assertFalse(envelope["success"])
            self.assertNotIn(str(self.root), run.stdout)
            self.assertNotIn("Traceback", run.stdout + run.stderr)
        run = subprocess.run([sys.executable, "-B", str(script), "--invalid", "--json"], capture_output=True, text=True)
        self.assertNotEqual(0, run.returncode)
        self.assertFalse(json.loads(run.stdout)["success"])

    def test_apply_prepares_only_exact_approved_request_without_side_effects(self):
        operative = self.approved()
        before = copy.deepcopy(operative)
        result = self.run_stage("apply", scope=self.scope(), sources=[self.source()],
                                operative_request=operative, attachment_requests=[])
        self.assertTrue(result["ok"])
        self.assertTrue(result["dispatch_required"])
        self.assertFalse(result["applied"])
        self.assertEqual(before, result["batch_request"])
        self.assertEqual(before, operative)
        self.assertEqual([], list(self.root.iterdir()))

    def test_apply_rejects_each_pending_or_tampered_approval(self):
        for gate in ("inner", "outer"):
            operative = self.approved()
            for state in ("pending", "tampered"):
                changed = copy.deepcopy(operative)
                target = changed["execute_request"]["review"] if gate == "inner" else changed["review"]
                if state == "pending":
                    target.clear()
                    target.update({"required": True, "state": "pending"})
                else:
                    target["approval_receipt"]["execute_request_sha256"] = "f" * 64
                with self.subTest(gate=gate, state=state):
                    self.assertFalse(self.run_stage("apply", scope=self.scope(), sources=[self.source()],
                        operative_request=changed)["ok"])

    def test_outer_approval_cannot_precede_inner_approval(self):
        operative = self.operative()
        operative["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:00:00Z", "reviewed_by": "fixture",
            "execute_request_sha256": dossier_hash(operative["execute_request"])}}
        self.assertFalse(self.review(operative=operative)["ok"])

    def test_apply_rejects_semantic_flags_without_claiming_completion(self):
        source = self.source()
        source["read"]["in_reply_to"] = "missing@example.test"
        self.assertTrue(self.review([source])["review_required"])
        self.assertFalse(self.run_stage("apply", scope=self.scope(), sources=[source],
            operative_request=self.approved())["ok"])

    def test_nested_unknown_fields_and_noncanonical_values_fail_closed(self):
        for section in ("search", "read", "attachments", "calendar"):
            source = self.source()
            if section == "attachments":
                source[section][0]["execute"] = True
            elif section == "calendar":
                source["calendar_objects"] = [{"uid": "a", "sequence": 1, "recurrence_id": None,
                    "content_sha256": "c" * 64, "execute": True}]
            else:
                source[section]["execute"] = True
            with self.subTest(section=section):
                self.assertFalse(self.review([source])["ok"])
        for schema in (True, 1.0, "1", None):
            self.assertFalse(self.api.process_request({"schema_version": schema, "stage": "plan", "scope": self.scope()},
                workspace_root=self.root)["ok"])
        self.assertFalse(self.run_stage("plan", scope={**self.scope(), "inspect_limit": float("nan")})["ok"])

    def test_attachments_preserve_every_location_and_reject_part_drift(self):
        first, second = self.source(), self.source("Archive", "9")
        second["attachments"][0]["original_filename"] = "alternate.pdf"
        result = self.review([first, second])
        self.assertTrue(result["ok"])
        self.assertEqual(2, len(result["attachments"][0]["provenance"]))
        drift = copy.deepcopy(first)
        drift["attachments"][0]["sha256"] = "c" * 64
        self.assertFalse(self.review([first, drift])["ok"])

    def test_attachment_requests_must_match_exact_inspected_inventory(self):
        source = self.source()
        attachment = copy.deepcopy(source["attachments"][0])
        self.assertTrue(self.run_stage("review", scope=self.scope(), sources=[source],
            operative_request=self.operative(), attachment_requests=[attachment])["ok"])
        attachment["part_locator"] = "3"
        self.assertFalse(self.run_stage("review", scope=self.scope(), sources=[source],
            operative_request=self.operative(), attachment_requests=[attachment])["ok"])

    def test_operational_sources_cannot_be_substituted_or_duplicated(self):
        for field, value in (("envelope_id", "2"), ("message_id", "other@example.test"), ("source_folder", "Archive")):
            operative = self.operative()
            operative["execute_request"]["items"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(self.review(operative=operative)["ok"])
        source = self.source()
        drift = self.source(mid="other@example.test")
        self.assertFalse(self.review([source, drift])["ok"])

    def test_handoff_rejects_result_hash_receipt_and_source_substitution(self):
        operative = self.approved()
        applied = self.applied(operative)
        self.assertFalse(self.handoff(operative, applied, apply_result_sha256="f" * 64)["ok"])
        for mutation in ("receipt", "execute", "partial"):
            value = copy.deepcopy(applied)
            if mutation == "receipt":
                value["approval_receipt"] = {**value["approval_receipt"], "reviewed_by": "other"}
                value["review"]["approval_receipt"] = value["approval_receipt"]
            elif mutation == "execute":
                value["execute_summary"]["results"][0]["message_id"] = "other@example.test"
            else:
                value["verify_summary"]["results"] = []
            with self.subTest(mutation=mutation):
                self.assertFalse(self.handoff(operative, value)["ok"])

    def test_handoff_does_not_include_unapplied_inspection_candidates(self):
        source = self.source("Archive", "9", "other@example.test", "d" * 64)
        source["read"]["content_sha256"] = "e" * 64
        result = self.handoff(sources=[self.source(), source])
        self.assertTrue(result["ok"])
        self.assertEqual(["source@example.test"], [s["message_id"] for s in result["work_order"]["sources"]])
        self.assertEqual(1, len(result["work_order"]["attachments"]))

    def test_handoff_rejects_post_apply_synthesis_target_drift(self):
        operative = self.approved()
        applied = self.applied(operative)
        target = [{"file": "memory/references/projects/demo/notes.md", "type": "project"}]
        applied["execute_summary"]["results"][0]["synthesis_targets"] = target
        applied["synthesis_handoff"]["items"][0]["synthesis_targets"] = target
        applied["synthesis_handoff"]["items"][0]["target_selection_required"] = False
        self.assertFalse(self.handoff(operative, applied)["ok"])

    def test_calendar_data_is_not_dropped_from_nonconflicting_work_order(self):
        source = self.source()
        calendar = {"uid": "calendar-a", "sequence": 1, "recurrence_id": None, "content_sha256": "c" * 64}
        source["calendar_objects"] = [calendar]
        result = self.handoff(sources=[source])
        self.assertTrue(result["ok"])
        self.assertEqual([calendar], result["work_order"]["sources"][0]["calendar_objects"])

    def output_fixture(self):
        import hashlib
        order = self.handoff()["work_order"]
        artifact = "memory/references/projects/demo/events/workshop/index.md"
        target = self.root / artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Synthetic workshop\n", encoding="utf-8")
        receipt = {"schema_version": 1, "work_order_sha256": canonical_json_sha256(order),
                   "source_snapshot_sha256": order["source_snapshot_sha256"],
                   "outputs": [{"path": artifact, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}]}
        return {"artifacts": [artifact], "work_order": order, "output_receipt": receipt}

    def test_report_rejects_content_snapshot_and_receipt_drift(self):
        for kind in ("content", "source", "order", "receipt", "partial", "missing"):
            request = self.output_fixture()
            if kind == "content":
                (self.root / request["artifacts"][0]).write_text("Changed", encoding="utf-8")
            elif kind == "source":
                request["work_order"]["sources"][0]["content_sha256"] = "f" * 64
            elif kind == "order":
                request["work_order"]["event"]["title"] = "Other event"
            elif kind == "receipt":
                request["output_receipt"]["work_order_sha256"] = "f" * 64
            elif kind == "partial":
                request["output_receipt"]["outputs"].append({"path": "memory/evidence/projects/demo/events/workshop/missing.json", "sha256": "f" * 64})
            else:
                (self.root / request["artifacts"][0]).unlink()
            with self.subTest(kind=kind):
                self.assertFalse(self.run_stage("report", **request)["ok"])

    def test_report_rejects_escape_cross_event_and_duplicate_paths(self):
        for path in ("../outside", "/outside", "C:/outside", "memory/references/projects/demo/events/other/index.md",
                     "memory/references/projects/demo/events/workshop/../other/index.md"):
            request = self.output_fixture()
            request["artifacts"] = [path]
            request["output_receipt"]["outputs"][0]["path"] = path
            with self.subTest(path=path):
                self.assertFalse(self.run_stage("report", **request)["ok"])
        request = self.output_fixture()
        request["artifacts"] *= 2
        self.assertFalse(self.run_stage("report", **request)["ok"])

    def test_report_rejects_symlink_output(self):
        request = self.output_fixture()
        target = self.root / request["artifacts"][0]
        outside = self.root / "outside.md"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("Platform does not permit synthetic symlinks")
        self.assertFalse(self.run_stage("report", **request)["ok"])

    def test_cli_success_is_canonical_and_unknown_parser_input_is_bounded(self):
        script = Path(__file__).resolve().parents[1] / "scripts/mail_desk_event_dossier.py"
        request = self.root / "request.json"
        request.write_text(json.dumps({"schema_version": 1, "stage": "plan", "scope": self.scope()}), encoding="utf-8")
        run = subprocess.run([sys.executable, "-B", str(script), "--request", str(request),
            "--workspace", str(self.root), "--json"], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        envelope = json.loads(run.stdout)
        self.assertEqual({"action", "success", "state", "message", "data", "error"}, set(envelope))
        self.assertTrue(envelope["success"])
        run = subprocess.run([sys.executable, "-B", str(script), "--" + "x" * 10000, "--json"], capture_output=True, text=True)
        self.assertNotEqual(0, run.returncode)
        self.assertLess(len(run.stdout), 1000)
        self.assertEqual("", run.stderr)

    def test_inspection_ids_are_bounded_and_folder_bound(self):
        self.assertFalse(self.run_stage("plan", scope=self.scope(), inspect_ids=["1"])["ok"])
        scope = {**self.scope(), "folders": ["INBOX"]}
        result = self.run_stage("plan", scope=scope, inspect_ids=["1"])
        self.assertTrue(result["ok"])
        self.assertEqual("INBOX", result["inspect_requests"][0]["folder"])
        self.assertFalse(self.run_stage("plan", scope=scope, inspect_ids=["1", "1"])["ok"])

    def test_oversized_api_and_cli_requests_fail_without_echoing_payload(self):
        self.assertFalse(self.run_stage("plan", scope=self.scope(), inspect_ids=["x" * (2 * 1024 * 1024)])["ok"])
        script = Path(__file__).resolve().parents[1] / "scripts/mail_desk_event_dossier.py"
        request = self.root / "large.json"
        request.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        run = subprocess.run([sys.executable, "-B", str(script), "--request", str(request),
            "--workspace", str(self.root), "--json"], capture_output=True, text=True)
        self.assertNotEqual(0, run.returncode)
        self.assertLess(len(run.stdout), 1000)
        self.assertFalse(json.loads(run.stdout)["success"])

    def test_nested_json_type_mutations_never_escape_error_boundary(self):
        def paths(value, prefix=()):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield prefix + (key,)
                    yield from paths(child, prefix + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    yield prefix + (index,)
                    yield from paths(child, prefix + (index,))
        operative = self.approved()
        applied = self.applied(operative)
        requests = [
            {"schema_version": 1, "stage": "review", "scope": self.scope(), "sources": [self.source()],
             "operative_request": operative, "attachment_requests": []},
            {"schema_version": 1, "stage": "handoff", "scope": self.scope(), "sources": [self.source()],
             "apply_request": operative, "apply_result": applied, "apply_result_sha256": canonical_json_sha256(applied),
             "event": {"owner_kind": "project", "owner_id": "demo", "id": "workshop",
                       "title": "Workshop", "start": "2026-01-01"}},
            {"schema_version": 1, "stage": "report", **self.output_fixture()},
        ]
        for request in requests:
            for path in paths(request):
                for replacement in (None, True, 7, "", [], {}):
                    changed = copy.deepcopy(request)
                    node = changed
                    for key in path[:-1]:
                        node = node[key]
                    node[path[-1]] = replacement
                    with self.subTest(stage=request["stage"], path=path, replacement=replacement):
                        result = self.api.process_request(changed, workspace_root=self.root)
                        self.assertIsInstance(result, dict)
                        self.assertIsInstance(result["ok"], bool)

    def test_report_rejects_hardlink_and_reparse_parent(self):
        import os
        from types import SimpleNamespace
        from unittest import mock
        request = self.output_fixture()
        target = self.root / request["artifacts"][0]
        linked = self.root / "alias.md"
        os.link(target, linked)
        self.assertFalse(self.run_stage("report", **request)["ok"])
        linked.unlink()
        # The facade resolves the declared root (including Windows 8.3 aliases).
        # Inject at that canonical path and assert the guard actually saw it.
        parent = target.parent.resolve()
        injected = []
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == parent:
                injected.append(path)
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with mock.patch.object(Path, "lstat", lstat):
            self.assertFalse(self.run_stage("report", **request)["ok"])
        self.assertTrue(injected)
        self.assertTrue(self.run_stage("report", **request)["ok"])
