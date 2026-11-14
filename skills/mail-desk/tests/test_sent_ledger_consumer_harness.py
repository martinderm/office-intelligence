"""RED-gate driver for the declared consumer fixture harness (Issue #88 / G1).

Covers the eight declared start cases F1, F2, F3, F5, F17, F19, F21 and F22 from
``runs/office-intelligence/2026-11-12-lane-88-consumer-rollout/offline-acceptance.md``
§4 against the installed bundle adapter, using only synthetic data
(``example.org``, ``SYNTH-A``/``SYNTH-B``) and the instrumented transport stub of
``sent_ledger_consumer_harness``.

Two evidence classes are kept strictly apart:

* **real** - a lease acquired and released through the published ``workspace-lock``
  skill (``WORKSPACE_LOCK_ROOT`` / project-relative ``.agents/skills`` junction);
* **injected** - a verifier test double that replaces
  ``core.sent_ledger.verify_workspace_lock`` and proves nothing about a real lease.

``_bind_workspace_data_dir`` is never patched, so the target binding stays
fail-closed in both classes.  No mailbox, network or real mail is touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

from core import himalaya  # noqa: E402
from core.modes.sync_sent import run_sync_sent_mode  # noqa: E402
from sent_ledger_consumer_harness import (  # noqa: E402
    ACCOUNT_A,
    ACCOUNT_B,
    DECLARED_TOOLS,
    HARNESS_CASE_IDS,
    HARNESS_KIND,
    LEDGER_SUBCOMMANDS,
    SYNTH_DAY,
    SYNTH_DAY_NEXT,
    SYNTH_FOLDER,
    InjectedLease,
    RealLease,
    SyntheticWorkspace,
    TransportStub,
    assert_no_host_paths,
    capability_probe,
    consume,
    declared_subcommands,
    handoff_item,
    item_ref,
    ledger_line,
    pending,
    record,
    run_case,
    run_cli_process,
    synthetic_detail,
    synthetic_envelope,
    workspace_lock_root,
)

WORKSPACE_LOCK = workspace_lock_root()

REAL_LEASE_REASON = (
    "the published workspace-lock skill is not installed on this host, so no real "
    "lease can be acquired; a skip is not a pass (F2 stays open)"
)

MANIFEST_ITEMS = (
    "kind", "view_version", "read_only", "status", "handoff_state", "handoff_present",
    "handoff_item_count", "pending_count", "consumed_ignored", "consumed_outcomes",
    "consumed_sources", "counts", "ledger", "items",
)


@contextlib.contextmanager
def lease_environment(root: Path | None):
    """Expose exactly one guard root and no ambient lease/conversation IDs."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in ("WORKSPACE_LOCK_LEASE_ID", "WORKSPACE_LOCK_CONVERSATION_ID"):
            os.environ.pop(name, None)
        if root is None:
            os.environ.pop("WORKSPACE_LOCK_ROOT", None)
        else:
            os.environ["WORKSPACE_LOCK_ROOT"] = str(root)
        yield


class HarnessTestCase(unittest.TestCase):
    """Shared isolated workspace factory; nothing is written outside the temp root."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def workspace(
        self,
        name: str = "synth-a",
        *,
        account: str | None = ACCOUNT_A,
        handoff_items: list[dict] | None = None,
        ledger_lines: list[dict] | None = None,
        handoff_missing: bool = False,
    ) -> SyntheticWorkspace:
        return SyntheticWorkspace.create(
            self.root / name,
            account=account,
            handoff_items=handoff_items,
            ledger_lines=ledger_lines,
            handoff_missing=handoff_missing,
        )

    def assert_unchanged(self, before: dict[str, str], workspace: SyntheticWorkspace) -> None:
        after = workspace.snapshot()
        self.assertEqual(
            after, before,
            msg=f"unexpected writes under the isolated workspace: {sorted(set(after) ^ set(before))}",
        )


class F1CapabilityProbeTest(HarnessTestCase):
    """F1: the capability probe finds every declared symbol, schema and CLI switch."""

    def test_f1_probe_reports_all_symbols_schema_and_cli_surface(self) -> None:
        probe = capability_probe()
        self.assertEqual(probe.missing_symbols, (), msg="declared seam symbols are missing")
        self.assertEqual(probe.ledger_schema_version, 1)
        self.assertEqual(probe.ledger_outcomes, ("synthesized", "no_evidence"))
        self.assertEqual(probe.ledger_sources, ("synthesis", "legacy_import"))
        self.assertEqual(probe.missing_commands, (), msg="declared CLI subcommands are missing")
        for name in LEDGER_SUBCOMMANDS:
            self.assertTrue(probe.cli_json_flag.get(name), msg=f"--json missing on {name}")
        self.assertTrue(probe.ok)

    def test_f1_probe_evidence_is_path_free_and_names_the_installed_commit(self) -> None:
        evidence = capability_probe().as_evidence()
        assert_no_host_paths(evidence)
        self.assertEqual(evidence["harness_kind"], HARNESS_KIND)
        commit = evidence["install_commit"]
        self.assertTrue(
            commit is None or (len(commit) == 40 and set(commit) <= set("0123456789abcdef")),
            msg="the probe must name a full commit id or an explicit None",
        )


class F2LeaseTest(HarnessTestCase):
    """F2: read-only needs no lease; mutating paths need a real, owned, active lease."""

    def test_f2_pending_reads_without_any_lease(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        before = workspace.snapshot()
        with lease_environment(None):
            result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        self.assertTrue(result.data.get("read_only"))
        self.assertEqual(result.data.get("pending_count"), 1)
        self.assertFalse(workspace.session_lock.exists(), msg="a read took a lease")
        self.assert_unchanged(before, workspace)

    @unittest.skipUnless(WORKSPACE_LOCK is not None, REAL_LEASE_REASON)
    def test_f2_real_lease_authorizes_the_mutating_path_and_is_released(self) -> None:
        workspace = self.workspace(handoff_items=[
            handoff_item("alpha@example.org"),
            handoff_item("beta@example.org"),
        ])
        lease = RealLease(workspace.root, "LEASE-OI88-REAL-A")
        lease.acquire()
        self.addCleanup(lease.release)
        self.assertTrue(workspace.session_lock.is_file(), msg="no real lease artifact")

        with lease_environment(WORKSPACE_LOCK):
            first = record(
                workspace, "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="alpha@example.org", lease_id=lease.lease_id,
            )
            self.assertEqual(first.exit_code, 0, msg=json.dumps(first.envelope))
            self.assertEqual(first.data.get("appended"), 1)

            foreign = record(
                workspace, "--outcome", "synthesized", "--vorgang-ref", "V-2",
                message_id="beta@example.org", lease_id="LEASE-OI88-FOREIGN",
            )
            self.assertEqual(foreign.exit_code, 2, msg="a foreign lease authorized a write")
            self.assertEqual(foreign.reason_code, "workspace_lease_required")

            lease.release()
            after_foreign = workspace.snapshot()
            after_release = record(
                workspace, "--outcome", "synthesized", "--vorgang-ref", "V-2",
                message_id="beta@example.org", lease_id=lease.lease_id,
            )
            self.assertEqual(after_release.exit_code, 2, msg="a released lease authorized a write")
            self.assertEqual(after_release.reason_code, "workspace_lease_required")
            self.assert_unchanged(after_foreign, workspace)
        evidence = lease.as_evidence()
        self.assertEqual(evidence["lease_source"], "real")
        self.assertTrue(evidence["released"])
        self.assertFalse(evidence["artifact_present_after_release"])
        self.assertEqual(evidence["artifact"], ".agents/session.lock")
        self.assertTrue(evidence["acquired_at"] and evidence["released_at"])
        self.assertEqual(len(workspace.ledger_lines()), 1, msg="exactly one authorized append")

    def test_f2_injected_lease_is_evidence_of_nothing_real(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        injected = InjectedLease()
        with injected, lease_environment(None):
            result = record(
                workspace, "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="alpha@example.org", lease_id=injected.lease_id,
            )
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        self.assertEqual(result.data.get("appended"), 1)
        evidence = injected.as_evidence()
        self.assertEqual(evidence["lease_source"], "injected")
        self.assertFalse(evidence["real_lease_proven"])
        self.assertFalse(
            workspace.session_lock.exists(),
            msg="an injected verifier must never leave a real lease artifact",
        )

    def test_f2_injected_denials_block_every_mutating_path_without_write(self) -> None:
        evaluations = self.workspace(
            "synth-consume", handoff_items=[handoff_item("alpha@example.org")],
        ).evaluations([{"message_id": "alpha@example.org", "outcome": "synthesized",
                        "vorgang_ref": "V-1"}])
        for mode in ("missing", "expired", "foreign"):
            with self.subTest(lease=mode):
                workspace = self.workspace(
                    f"synth-{mode}", handoff_items=[handoff_item("alpha@example.org")],
                )
                before = workspace.snapshot()
                injected = InjectedLease(mode=mode)
                runner = run_case(
                    "F2", "a denied lease stops before any write", lease=injected,
                )
                with injected, lease_environment(None):
                    recorded = record(
                        workspace, "--outcome", "synthesized", "--vorgang-ref", "V-1",
                        message_id="alpha@example.org", lease_id=injected.lease_id,
                    )
                    consumed = consume(
                        workspace, evaluations, lease_id=injected.lease_id,
                    )
                for result in (recorded, consumed):
                    self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
                    self.assertEqual(result.reason_code, "workspace_lease_required")
                evidence = runner.finish(
                    result=recorded,
                    snapshot_before=before,
                    snapshot_after=workspace.snapshot(),
                )
                self.assertTrue(evidence.files_unchanged, msg=str(evidence.changed_files))
                self.assertEqual(evidence.lease_source, "injected")


class F3TargetBindingTest(HarnessTestCase):
    """F3: a lease for workspace A never authorizes the data zone of workspace B."""

    def test_f3_foreign_data_dir_stops_a_mutating_call_before_read_and_write(self) -> None:
        workspace_a = self.workspace("synth-a", handoff_items=[handoff_item("alpha@example.org")])
        workspace_b = self.workspace("synth-b", handoff_items=[handoff_item("beta@example.org")])
        before_a, before_b = workspace_a.snapshot(), workspace_b.snapshot()
        with InjectedLease(), lease_environment(None):
            result = _record_into(
                workspace_a,
                workspace_b,
                "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="beta@example.org", lease_id="INJECTED-LEASE",
            )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assert_unchanged(before_a, workspace_a)
        self.assert_unchanged(before_b, workspace_b)

    def test_f3_read_only_pending_on_a_foreign_data_dir_stops_too(self) -> None:
        workspace_a = self.workspace("synth-a", handoff_items=[handoff_item("alpha@example.org")])
        workspace_b = self.workspace("synth-b", handoff_items=[handoff_item("beta@example.org")])
        before_a, before_b = workspace_a.snapshot(), workspace_b.snapshot()
        result = _run_against_foreign_data_dir(workspace_a, workspace_b, "pending")
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assert_unchanged(before_a, workspace_a)
        self.assert_unchanged(before_b, workspace_b)


def _run_against_foreign_data_dir(workspace_a, workspace_b, *args):
    """Run the ledger CLI with workspace A's root and workspace B's data zone."""
    from sent_ledger_consumer_harness import run_cli

    return run_cli(
        *args, "--workspace", str(workspace_a.root), "--data-dir", str(workspace_b.data_dir),
    )


def _record_into(workspace_a, workspace_b, *extra, message_id=None, lease_id=None):
    from sent_ledger_consumer_harness import run_cli

    selector = ["--message-id", message_id] if message_id else []
    lease = ["--workspace-lease-id", lease_id] if lease_id else []
    return run_cli(
        "record", "--workspace", str(workspace_a.root),
        "--data-dir", str(workspace_b.data_dir), *selector, *lease, *extra,
    )


class F5HandoffStateTest(HarnessTestCase):
    """F5: handoff state, presence and the documented ``data.*`` field paths."""

    def test_f5_missing_handoff_is_not_an_empty_handoff(self) -> None:
        workspace = self.workspace()
        self.assertFalse((workspace.data_dir / "new-sent-items.json").exists())
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        self.assertFalse(result.data["handoff_present"])
        self.assertEqual(result.data["handoff_item_count"], 0)
        self.assertEqual(result.data["pending_count"], 0)
        self.assertEqual(result.data["handoff_state"], "none")

    def test_f5_empty_handoff_is_present_but_empty(self) -> None:
        workspace = self.workspace(handoff_items=[])
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        self.assertTrue(result.data["handoff_present"])
        self.assertEqual(result.data["handoff_item_count"], 0)
        self.assertEqual(result.data["pending_count"], 0)
        self.assertEqual(result.data["handoff_state"], "none")

    def test_f5_two_open_items_expose_the_documented_field_paths(self) -> None:
        workspace = self.workspace(handoff_items=[
            handoff_item("alpha@example.org"),
            handoff_item("beta@example.org"),
        ])
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        data = result.data
        missing = [name for name in MANIFEST_ITEMS if name not in data]
        self.assertEqual(missing, [], msg=f"documented data.* field paths missing: {missing}")
        self.assertEqual(data["handoff_state"], "pending")
        self.assertEqual(data["handoff_item_count"], 2)
        self.assertEqual(data["pending_count"], 2)
        self.assertIsInstance(data["consumed_ignored"], int)
        self.assertEqual(
            set(data["consumed_outcomes"]), {"synthesized", "no_evidence"},
        )
        self.assertEqual(set(data["consumed_sources"]), {"synthesis", "legacy_import"})
        self.assertNotEqual(data["handoff_state"], "written")
        self.assertNotIn("Synthetic subject", json.dumps(result.envelope))
        self.assertNotIn("alpha@example.org", json.dumps(result.envelope))

    def test_f5_corrupt_handoff_is_exit_two_without_consumption(self) -> None:
        workspace = self.workspace()
        path = workspace.data_dir / "new-sent-items.json"
        path.write_text("{not json", encoding="utf-8")
        before = workspace.snapshot()
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertIn(result.reason_code, {"handoff_invalid", "ledger_invalid"})
        self.assert_unchanged(before, workspace)

    def test_f5_unknown_item_field_fails_closed(self) -> None:
        item = handoff_item("alpha@example.org")
        item["unknown_field"] = "synthetic"
        workspace = self.workspace(handoff_items=[item])
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "handoff_invalid")
        self.assertTrue(
            str(result.data.get("reason", "")).startswith("item[0]: unknown_field"),
            msg=str(result.data),
        )


class F17CountSemanticsTest(HarnessTestCase):
    """F17: ``count`` is a day-boundary item budget; the day is read completely."""

    def _stub(self, pages: dict) -> TransportStub:
        details = {}
        for day_pages in pages.values():
            for page in day_pages:
                for envelope in page:
                    env_id = str(envelope["id"])
                    details[env_id] = synthetic_detail(
                        env_id, f"sent-{env_id}@example.org",
                    )
        return TransportStub(pages=pages, details=details)

    def test_f17_budget_without_accepted_item_reads_the_day_and_stops(self) -> None:
        workspace = self.workspace()
        stub = self._stub({SYNTH_DAY: [[
            synthetic_envelope("91"), synthetic_envelope("92", has_attachment=True),
        ]]})
        with stub:
            self.addCleanup(stub.uninstall)
            envelope = run_sync_sent_mode(
                {"count": 1, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A,
                data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()

        self.assertEqual(
            len(stub.detail_requests), 2,
            msg=f"the bounded day was not read completely: {json.dumps(envelope)[:800]}",
        )
        for request in stub.detail_requests:
            self.assertEqual(request.preview_lines, 5)
        self.assertEqual(
            [request.date for request in stub.page_requests], [SYNTH_DAY],
            msg="a day beyond the budget must not be read",
        )
        self.assertTrue(envelope["truncated"])
        self.assertEqual(envelope["new_count"], 0)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["reason_code"], "sync_no_progress")
        self.assertEqual(envelope["write_state"], "state")
        self.assertFalse((workspace.data_dir / "new-sent-items.json").exists())
        self.assertFalse((workspace.data_dir / "sent-index.jsonl").exists())

    def test_f17_one_accepted_item_keeps_the_envelope_ok_but_truncated(self) -> None:
        workspace = self.workspace()
        stub = self._stub({
            SYNTH_DAY: [[synthetic_envelope("91")]],
            SYNTH_DAY_NEXT: [[synthetic_envelope("92"), synthetic_envelope("93")]],
        })
        with stub:
            self.addCleanup(stub.uninstall)
            envelope = run_sync_sent_mode(
                {"count": 1, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A,
                data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()

        self.assertEqual(len(stub.detail_requests), 3)
        self.assertTrue(envelope["ok"], msg=json.dumps(envelope)[:600])
        self.assertTrue(envelope["truncated"])
        self.assertEqual(envelope["new_count"], 1)
        self.assertEqual(envelope.get("next_start"), SYNTH_DAY_NEXT)
        self.assertEqual(len(workspace.ledger_lines()), 0, msg="the sync itself consumes nothing")

    def test_f17_preview_lines_bound_the_output_not_the_fetch(self) -> None:
        body = "\n".join(f"Synthetic body line {index}" for index in range(1, 9))
        stub = TransportStub(
            pages={},
            details={"91": synthetic_detail("91", "sent-91@example.org")},
            read_body=body,
        )
        with stub:
            self.addCleanup(stub.uninstall)
            with_attachment = himalaya.get_single_email_details(
                "91", SYNTH_FOLDER, account=ACCOUNT_A, preview_lines=5,
                fallback_envelope=synthetic_envelope("91", has_attachment=True),
            )
            without_attachment = himalaya.get_single_email_details(
                "92", SYNTH_FOLDER, account=ACCOUNT_A, preview_lines=5,
                fallback_envelope=synthetic_envelope("92", has_attachment=False),
            )
            stub.assert_no_real_transport()

        self.assertEqual(len(stub.raw_requests), 1, msg="the attachment path must fetch the raw EML")
        self.assertEqual(stub.raw_requests[0].envelope_id, "91")
        self.assertEqual(len(stub.requests_of("read")), 2, msg="each detail read is a preview read")
        self.assertEqual(len(with_attachment["preview"].splitlines()), 5)
        self.assertEqual(with_attachment["read_level"], "preview")
        self.assertEqual(len(without_attachment["preview"].splitlines()), 5)
        self.assertIsNone(without_attachment["attachment_error"])
        self.assertEqual(without_attachment["attachment_status"], "none")


class F19MailboxMismatchTest(HarnessTestCase):
    """F19: one foreign open item stops the whole round with hashed references only."""

    def test_f19_foreign_item_stops_the_round_before_any_write(self) -> None:
        workspace = self.workspace(handoff_items=[
            handoff_item("alpha@example.org", mailbox=ACCOUNT_A),
            handoff_item("beta@example.org", mailbox=ACCOUNT_B),
        ])
        before = workspace.snapshot()
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "mailbox_mismatch")
        self.assertEqual(result.data.get("mismatch_count"), 1)
        self.assertEqual(result.data.get("verified_account"), ACCOUNT_A)
        refs = result.data.get("mismatch_refs") or []
        self.assertEqual(refs, [item_ref("beta@example.org")])
        for reference in refs:
            self.assertRegex(reference, r"^sha256:[0-9a-f]{16}$")
        self.assertNotIn("beta@example.org", json.dumps(result.envelope))
        self.assert_unchanged(before, workspace)

    def test_f19_requested_account_must_match_the_workspace_binding(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        result = pending(workspace, "--account", ACCOUNT_B)
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "account_mismatch")

    def test_f19_corrected_mailbox_runs_normally(self) -> None:
        workspace = self.workspace(handoff_items=[
            handoff_item("alpha@example.org", mailbox=ACCOUNT_A),
            handoff_item("beta@example.org", mailbox=ACCOUNT_B),
        ])
        workspace.write_handoff([
            handoff_item("alpha@example.org", mailbox=ACCOUNT_A),
            handoff_item("beta@example.org", mailbox=ACCOUNT_A),
        ])
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        self.assertEqual(result.data.get("pending_count"), 2)


class F21DeclaredToolsTest(HarnessTestCase):
    """F21: the declared inventory tools and the fixture harness exist and are named."""

    def test_f21_legacy_inventory_subcommand_is_declared(self) -> None:
        commands = declared_subcommands()
        self.assertIn(
            "inventory-legacy", commands,
            msg="the declared legacy-inventory tool is missing from the CLI surface "
                f"(declared: {sorted(commands)})",
        )

    def test_f21_promotion_inventory_subcommand_is_declared(self) -> None:
        commands = declared_subcommands()
        self.assertIn(
            "inventory-promotion", commands,
            msg="the declared promotion-evidence-inventory tool is missing from the CLI surface "
                f"(declared: {sorted(commands)})",
        )

    def test_f21_declared_inventory_tools_answer_help(self) -> None:
        for tool in ("inventory-legacy", "inventory-promotion"):
            with self.subTest(tool=tool):
                completed = run_cli_process(tool, "--help")
                self.assertEqual(
                    completed.returncode, 0,
                    msg=f"{tool} is not invocable: {(completed.stderr or '').strip()[:300]}",
                )

    def test_f21_legacy_inventory_runs_read_only_without_raw_identifiers(self) -> None:
        workspace = self.workspace()
        inventory = workspace.data_dir / "sent-evidence-index.json"
        inventory.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in (
                    {
                        "schema_version": 1, "message_id": "alpha@example.org",
                        "status": "synthesized", "vorgang_ref": "V-1", "evid_id": "E-1",
                        "recorded_at": "2026-11-01T09:00:00Z",
                    },
                    {
                        "schema_version": 1, "message_id": "beta@example.org",
                        "status": "no_evidence", "reason_code": "no_relevant_evidence",
                        "recorded_at": "2026-11-01T09:05:00Z",
                    },
                )
            ),
            encoding="utf-8",
        )
        before = workspace.snapshot()
        completed = run_cli_process(
            "inventory-legacy", "--workspace", str(workspace.root),
            "--data-dir", "data/mail-desk", "--inventory", str(inventory), "--json",
        )
        self.assertEqual(
            completed.returncode, 0,
            msg="inventory-legacy is not a declared, runnable read-only tool: "
                + (completed.stderr or "").strip()[:400],
        )
        self.assertNotIn("alpha@example.org", completed.stdout)
        self.assertNotIn("beta@example.org", completed.stdout)
        self.assert_unchanged(before, workspace)

    def test_f21_harness_is_declared_without_inline_python_or_heredoc(self) -> None:
        declared = {tool["id"] for tool in DECLARED_TOOLS}
        self.assertEqual(
            {"fixture-harness", "capability-probe", "inventory-legacy", "inventory-promotion"},
            declared,
        )
        self.assertEqual(
            set(HARNESS_CASE_IDS),
            {"F1", "F2", "F3", "F5", "F17", "F19", "F21", "F22"},
        )
        for tool in DECLARED_TOOLS:
            entry = tool["entry"]
            self.assertNotIn(" -c ", entry, msg=f"inline python in declared tool {tool['id']}")
            self.assertNotIn("<<", entry, msg=f"heredoc in declared tool {tool['id']}")
        harness_entry = next(tool for tool in DECLARED_TOOLS if tool["id"] == "fixture-harness")
        self.assertIn("-m unittest", harness_entry["entry"])


class F22BatchConsumeTest(HarnessTestCase):
    """F22: ``consume`` appends one line per evaluated item and stays idempotent."""

    ANCESTOR = "alpha@example.org"
    NO_EVIDENCE = "beta@example.org"
    NOT_EVALUATED = "gamma@example.org"
    REPLY = "delta@example.org"

    def _workspace(self, name: str = "synth-a") -> SyntheticWorkspace:
        return self.workspace(name, handoff_items=[
            handoff_item(self.REPLY, in_reply_to=self.ANCESTOR),
            handoff_item(self.ANCESTOR),
            handoff_item(self.NO_EVIDENCE),
            handoff_item(self.NOT_EVALUATED),
        ])

    def _evaluations(self, workspace: SyntheticWorkspace) -> Path:
        return workspace.evaluations([
            {
                "item_ref": item_ref(self.ANCESTOR), "outcome": "synthesized",
                "vorgang_ref": "V-1", "evid_id": "E-1",
            },
            {
                "message_id": self.NO_EVIDENCE, "outcome": "no_evidence",
                "reason_code": "no_relevant_evidence",
            },
            {
                "message_id": self.REPLY, "outcome": "synthesized",
                "vorgang_ref": "V-1", "evid_id": "E-2",
            },
        ])

    def test_f22_dry_run_needs_no_lease_and_writes_nothing(self) -> None:
        workspace = self._workspace()
        evaluations = self._evaluations(workspace)
        before = workspace.snapshot()
        injected = InjectedLease(mode="deny")
        runner = run_case(
            "F22", "dry run projects without a lease or a write", lease=injected,
        )
        with injected, lease_environment(None):
            result = consume(workspace, evaluations, "--dry-run")
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        self.assertTrue(result.data.get("dry_run"))
        self.assertEqual(result.data.get("would_consume"), 3)
        self.assertEqual(result.data.get("projected_pending_after"), 1)
        self.assertEqual(result.data.get("appended"), 0)
        self.assertEqual(len(result.data.get("not_evaluated") or []), 1)
        self.assertFalse(workspace.session_lock.exists())
        evidence = runner.finish(
            result=result, snapshot_before=before, snapshot_after=workspace.snapshot(),
        )
        self.assertTrue(evidence.files_unchanged, msg=str(evidence.changed_files))

    def test_f22_lease_round_appends_one_line_per_item_and_is_idempotent(self) -> None:
        workspace = self._workspace()
        evaluations = self._evaluations(workspace)
        handoff_before = workspace.handoff_bytes()
        injected = InjectedLease()
        with injected, lease_environment(None):
            first = consume(workspace, evaluations, lease_id=injected.lease_id)
            self.assertEqual(first.exit_code, 0, msg=json.dumps(first.envelope))
            self.assertEqual(first.data.get("appended"), 3)
            self.assertEqual(first.data.get("pending_after"), 1)
            self.assertEqual(len(first.data.get("not_evaluated") or []), 1)

            lines = workspace.ledger_lines()
            self.assertEqual(len(lines), 3)
            by_message_id = {line["message_id"]: line for line in lines}
            self.assertEqual(by_message_id[self.NO_EVIDENCE]["outcome"], "no_evidence")
            self.assertEqual(by_message_id[self.NO_EVIDENCE]["reason_code"], "no_relevant_evidence")
            self.assertIsNone(by_message_id[self.NO_EVIDENCE].get("vorgang_ref"))
            self.assertIsNone(by_message_id[self.NO_EVIDENCE].get("anchored_to"))
            self.assertNotIn("evid_id", by_message_id[self.NO_EVIDENCE])
            self.assertEqual(by_message_id[self.REPLY]["role"], "follow_up")
            self.assertEqual(by_message_id[self.REPLY]["anchored_to"], self.ANCESTOR)
            order = [line["message_id"] for line in lines]
            self.assertLess(order.index(self.ANCESTOR), order.index(self.REPLY))

            second = consume(workspace, evaluations, lease_id=injected.lease_id)
        self.assertEqual(second.exit_code, 0, msg=json.dumps(second.envelope))
        self.assertEqual(second.data.get("appended"), 0)
        self.assertEqual(second.data.get("already_consumed_count"), 3)
        self.assertEqual(len(workspace.ledger_lines()), 3, msg="the rerun duplicated ledger lines")
        self.assertEqual(workspace.handoff_bytes(), handoff_before, msg="the handoff was rewritten")
        self.assertNotIn("alpha@example.org", json.dumps(second.envelope))

    def test_f22_duplicate_target_stops_before_the_first_write(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item(self.ANCESTOR)])
        evaluations = workspace.evaluations([
            {"item_ref": item_ref(self.ANCESTOR), "outcome": "synthesized", "vorgang_ref": "V-1"},
            {"message_id": self.ANCESTOR, "outcome": "synthesized", "vorgang_ref": "V-1"},
        ])
        before = workspace.snapshot()
        with InjectedLease(), lease_environment(None):
            result = consume(workspace, evaluations, lease_id="INJECTED-LEASE")
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "evaluation_duplicate")
        self.assert_unchanged(before, workspace)

    def test_f22_shared_evid_stops_before_the_first_write(self) -> None:
        workspace = self.workspace(handoff_items=[
            handoff_item(self.ANCESTOR),
            handoff_item(self.REPLY, in_reply_to=self.ANCESTOR),
        ])
        evaluations = workspace.evaluations([
            {"message_id": self.ANCESTOR, "outcome": "synthesized",
             "vorgang_ref": "V-1", "evid_id": "E-SHARED"},
            {"message_id": self.REPLY, "outcome": "synthesized",
             "vorgang_ref": "V-1", "evid_id": "E-SHARED"},
        ])
        before = workspace.snapshot()
        with InjectedLease(), lease_environment(None):
            result = consume(workspace, evaluations, lease_id="INJECTED-LEASE")
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "evid_shared_conflict")
        self.assert_unchanged(before, workspace)

    def test_f22_foreign_thread_anchor_blocks_the_round_as_consume_blocked(self) -> None:
        workspace = self.workspace(
            handoff_items=[handoff_item(self.REPLY, in_reply_to=self.ANCESTOR)],
            ledger_lines=[ledger_line(self.ANCESTOR, vorgang_ref="V-9", evid_id="E-9")],
        )
        evaluations = workspace.evaluations([
            {"message_id": self.REPLY, "outcome": "synthesized",
             "vorgang_ref": "V-2", "evid_id": "E-2"},
        ])
        before = workspace.snapshot()
        with InjectedLease(), lease_environment(None):
            result = consume(workspace, evaluations, lease_id="INJECTED-LEASE")
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "consume_blocked")
        blocked = result.data.get("blocked") or []
        self.assertEqual([entry.get("reason_code") for entry in blocked], ["anchor_conflict"])
        self.assert_unchanged(before, workspace)


if __name__ == "__main__":  # pragma: no cover - manual single-module run
    unittest.main()
