"""G1 follow-up cases for the declared consumer fixture harness (Issue #88).

Covers the remaining G1 cases named in
``runs/office-intelligence/2026-11-12-lane-88-consumer-rollout/offline-acceptance.md``
§4 — **F4, F6, F7, F8, F9, F10, F15, F16, F18 and F20** — against the installed
bundle adapter, using only synthetic data (``example.org``, ``SYNTH-A``/``SYNTH-B``)
and the declared harness of ``sent_ledger_consumer_harness``.

Deliberate boundaries
---------------------
* ``HARNESS_CASE_IDS`` (the reviewed start set F1/F2/F3/F5/F17/F19/F21/F22) is
  **not** touched; the follow-up cases are declared separately as
  ``G1_REMAINING_CASE_IDS``.
* F6-F8 are driven by an explicit *synthetic SOP branch model* inside this test
  module.  The model is a fixture, not product code: it encodes the documented
  phase order (pending gate **before** any draft, branch **immediately after a
  validated draft**, never after execute) and is fed with real adapter output
  (``pending`` views and ``sync_sent`` envelopes).
* F9, F10, F16 and F18 use the existing adapter paths ``pending``, ``record``,
  ``crash-check`` and ``recover-promotion``; F20 uses the declared tools
  ``inventory-legacy`` plus ``plan``/``apply``.
* No recovery is authorized without a proven-complete evidence list and a
  ``crash-check`` that classifies the triad as ``unique`` **and** reports
  ``safe_to_append: true``.  Without a required append, ``already_consumed`` is
  the accepted consumer no-op (exit 0, no recovery call, no write);
  ``safe_to_append: false`` is not a trigger.  F18(b) and F18(h) record that the
  mutating call was omitted.
* F10 is adapter-seam evidence only.  F10(b) proves the append-side conflict at
  the real ``recover-promotion`` seam: the durable promotion record exists (a
  synthetic fixture record, not a consumer decision), ``crash-check`` authorizes
  the append, the ledger anchor then changes, and the recovery append is refused
  with nothing written.  The preceding consumer promotion *decision* still has no
  declared production seam, so F10(a) and the promotion decision itself stay
  partial; no consumer implementation is modelled.
* F18(h) is explicitly a *synthetic consumer no-op model*: it is labelled as such
  and is never claimed as an observed production consumer envelope.  F18(c) is an
  adapter-boundary refusal probe and stays partial; neither is a full consumer
  flow.  ``link_kind`` carries an explicit Python < 3.12 fallback
  (``st_reparse_tag``) so the fixture does not depend on ``os.path.isjunction``.
* F20 binds the declared ``inventory-legacy`` report to ``plan``/``apply``
  counter by counter.  The report is not a plan input format and the adapter
  declares no such seam, so the binding is asserted rather than fed through.
* Every mutating call runs under an **injected** lease (a test double) and is
  labelled ``injected``; nothing here is evidence of a real lease (F2 owns that).
* Windows file-symlink capability is *not* required and is not claimed: F4 uses a
  real NTFS directory junction created with ``cmd /c mklink /J``.  A symlink is
  deliberately never substituted; the five historical Windows *file*-symlink
  cases (``test_sent_handoff.py``/``test_batch_cli_wrapper.py``) stay skipped and
  are not counted as passes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(TESTS_ROOT))

from core.modes.sync_sent import run_sync_sent_mode  # noqa: E402
from sent_ledger_consumer_harness import (  # noqa: E402
    ACCOUNT_A,
    CONSUMER_RECOVERY_GATE,
    F18_VARIANTS,
    G1_REMAINING_CASE_IDS,
    HARNESS_CASE_IDS,
    SYNTH_DAY,
    SYNTH_DAY_NEXT,
    SYNTH_FOLDER,
    InjectedLease,
    SyntheticWorkspace,
    TransportStub,
    assert_no_host_paths,
    consume,
    consumer_gate_problems,
    consumer_recovery_gate,
    crash_check,
    handoff_item,
    inventory_legacy,
    item_ref,
    legacy_apply,
    legacy_plan,
    ledger_line,
    link_kind,
    make_directory_link,
    pending,
    record,
    recover_promotion,
    run_case,
    run_cli,
    synthetic_detail,
    synthetic_envelope,
)

LEDGER_REL = "data/mail-desk/sent-synthesis-ledger.jsonl"

#: Phases that must never appear while a pending item is open.
GATED_PHASES = ("review", "sign", "execute", "completion", "manifest")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _pending_state(workspace: SyntheticWorkspace, account: str | None = ACCOUNT_A) -> dict:
    """Model input derived from the real adapter output (no invented state)."""
    extra = ["--account", account] if account else []
    result = pending(workspace, *extra)
    if result.exit_code != 0:
        return {
            "handoff_state": "invalid",
            "handoff_present": True,
            "pending_count": None,
            "exit_code": result.exit_code,
            "reason_code": result.reason_code,
        }
    return {
        "handoff_state": result.data["handoff_state"],
        "handoff_present": bool(result.data["handoff_present"]),
        "pending_count": result.data["pending_count"],
        "exit_code": 0,
        "reason_code": None,
    }


# ---------------------------------------------------------------------------
# Synthetic SOP branch model (F6-F8); fixture, not product code
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SopState:
    """One SOP decision input: inbox count plus the real pending/handoff state."""

    inbox_new: int
    handoff_state: str
    handoff_present: bool
    pending_count: int | None
    documented_items: int = 0
    draft_validated: bool = False
    fresh_pending_after_draft: int | None = None


@dataclass(frozen=True)
class SopDecision:
    """The branch decision and its phase trace."""

    decision: str
    trace: tuple[str, ...]

    def index(self, phase: str) -> int:
        return list(self.trace).index(phase)

    @property
    def gated_phases_present(self) -> tuple[str, ...]:
        return tuple(phase for phase in GATED_PHASES if phase in self.trace)


def sop_branch(state: SopState) -> SopDecision:
    """Model the documented SOP order (offline-acceptance §4 F6-F8).

    Order: phase-1 sync, pending gate **before** any draft, draft and its
    validation, then the branch **immediately after** the validated draft.  A
    pending item stops the round before draft/review/execute; a fresh pending item
    discovered after draft validation stops it at the branch, never after execute.
    """
    trace = ["phase1_sync"]
    if state.handoff_state in {"invalid", "unknown"} or state.pending_count is None:
        return SopDecision("stop_handoff_invalid", (*trace, "pending_gate_pre_draft", "stop"))
    trace.append("pending_gate_pre_draft")
    if not state.handoff_present and state.documented_items and state.pending_count == 0:
        # A documented item vanished: never a no-op proof, stop and investigate.
        return SopDecision("stop_unexpected_loss", (*trace, "stop"))
    if state.pending_count > 0:
        trace.append("consume_pending_6f")
        if state.draft_validated:
            return SopDecision("stop_pending_before_draft", (*trace, "draft_validated", "stop"))
        if state.inbox_new > 0:
            return SopDecision("stop_consume_first_then_resync", (*trace, "stop"))
        return SopDecision("consume_first_no_execute", tuple(trace))
    if state.inbox_new == 0:
        if not state.handoff_present:
            return SopDecision(
                "sync_authorized_not_noop_proof", (*trace, "sync_allowed_not_noop_proof"),
            )
        return SopDecision("no_op", (*trace, "no_op"))
    if not state.draft_validated:
        return SopDecision("inbound_outbound_noop", (*trace, "inbound_path", "outbound_noop"))
    trace.extend(["inbound_path", "draft", "draft_validated", "pending_gate_post_draft"])
    fresh = state.fresh_pending_after_draft or 0
    if fresh > 0:
        return SopDecision(
            "stop_pending_after_draft", (*trace, "consume_pending_6f", "stop"),
        )
    trace.extend(["review", "sign", "execute", "completion", "manifest"])
    return SopDecision("outbound_proceed", tuple(trace))


# ---------------------------------------------------------------------------
# Base fixture
# ---------------------------------------------------------------------------

class G1RemainingCase(unittest.TestCase):
    """Isolated temporary workspace per test; nothing is written outside it."""

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
        self.assertEqual(
            workspace.snapshot(), before,
            msg="the fixture wrote outside the expected files: "
                f"{sorted(set(workspace.snapshot()) ^ set(before))}",
        )

    def changed_files(self, before: dict[str, str], workspace: SyntheticWorkspace) -> list[str]:
        """Value-level diff of two snapshots (equal key sets are not 'unchanged')."""
        after = workspace.snapshot()
        return sorted(
            name for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        )

    def ledger_lines(self, workspace: SyntheticWorkspace) -> list[dict]:
        return _read_jsonl(workspace.data_dir / "sent-synthesis-ledger.jsonl")


class CaseDeclarationTest(G1RemainingCase):
    """The reviewed start set stays byte-identical; follow-up cases are separate."""

    def test_harness_start_cases_are_unchanged(self) -> None:
        self.assertEqual(
            HARNESS_CASE_IDS,
            ("F1", "F2", "F3", "F5", "F17", "F19", "F21", "F22"),
        )

    def test_remaining_case_ids_are_declared_separately(self) -> None:
        self.assertEqual(
            G1_REMAINING_CASE_IDS,
            ("F4", "F6", "F7", "F8", "F9", "F10", "F15", "F16", "F18", "F20"),
        )
        self.assertEqual(F18_VARIANTS, tuple("abcdefghijkl"))


# ---------------------------------------------------------------------------
# F4 — traversal / reparse component in the data-dir
# ---------------------------------------------------------------------------

class F4TraversalReparseTest(G1RemainingCase):
    """F4: a junction or a traversal stops before read/write and deletes nothing."""

    JUNCTION_SKIP = (
        "a real NTFS directory junction (cmd /c mklink /J) could not be created on this "
        "host; a symlink is not substituted because it has different semantics, so F4 "
        "stays open (skip is not a pass)"
    )

    def _linked_workspace(self) -> tuple[SyntheticWorkspace, SyntheticWorkspace]:
        external = self.workspace("external", handoff_items=[
            handoff_item("alpha@example.org"),
        ])
        linked = self.workspace("linked")
        shutil.rmtree(linked.data_dir)
        kind = make_directory_link(linked.data_dir, external.data_dir)
        if kind != "junction":
            self.skipTest(self.JUNCTION_SKIP)
        self.assertEqual(kind, "junction", msg="a symlink must never stand in for a junction")
        return linked, external

    def test_f4_junction_data_zone_is_refused_before_a_read(self) -> None:
        linked, external = self._linked_workspace()
        before = external.snapshot()
        evidence = run_case(
            "F4", "a junction data zone is refused before the read",
            details={"link_kind": "junction", "entry": "pending"},
        )
        result = pending(linked, "--account", ACCOUNT_A)
        record_evidence = evidence.finish(
            result=result, snapshot_before=before, snapshot_after=external.snapshot(),
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assertEqual(result.data.get("reason"), "linked_data_zone")
        self.assertIn(result.data.get("link_kind"), {"junction", "reparse"})
        self.assertEqual(result.data.get("component"), "mail-desk")
        self.assertNotIn("alpha@example.org", json.dumps(result.envelope))
        self.assertTrue(record_evidence.files_unchanged, msg=str(record_evidence.changed_files))
        self.assertEqual(link_kind(linked.data_dir), "junction", msg="the junction was deleted")
        self.assertTrue(external.data_dir.is_dir(), msg="the link target was removed")

    def test_f4_junction_data_zone_is_refused_before_a_write(self) -> None:
        linked, external = self._linked_workspace()
        before = external.snapshot()
        injected = InjectedLease()
        evidence = run_case(
            "F4", "a junction data zone is refused before the lease verifier and any write",
            lease=injected,
        )
        with injected:
            result = record(
                linked, "--outcome", "synthesized", "--vorgang-ref", "V-1",
                message_id="alpha@example.org", lease_id=injected.lease_id,
            )
        record_evidence = evidence.finish(
            result=result, snapshot_before=before, snapshot_after=external.snapshot(),
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assertEqual(result.data.get("reason"), "linked_data_zone")
        self.assertEqual(injected.calls, [], msg="the lease verifier ran after a refused binding")
        self.assertTrue(record_evidence.files_unchanged, msg=str(record_evidence.changed_files))
        self.assertFalse((external.data_dir / "sent-synthesis-ledger.jsonl").exists())
        self.assertEqual(link_kind(linked.data_dir), "junction")

    def test_f4_dotdot_traversal_out_of_the_data_zone_is_refused(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        escaped = workspace.root / "other" / "mail-desk"
        escaped.mkdir(parents=True)
        traversal = workspace.root / "data" / "mail-desk" / ".." / ".." / "other" / "mail-desk"
        before_ws = workspace.snapshot()
        evidence = run_case(
            "F4", "a '..' escape out of the data zone is refused before read/write",
            details={"entry": "pending", "reason": "data_zone_mismatch"},
        )
        result = run_cli(
            "pending", "--workspace", str(workspace.root),
            "--data-dir", str(traversal), "--account", ACCOUNT_A,
        )
        record_evidence = evidence.finish(
            result=result, snapshot_before=before_ws, snapshot_after=workspace.snapshot(),
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "workspace_binding_invalid")
        self.assertEqual(result.data.get("reason"), "data_zone_mismatch")
        self.assertTrue(record_evidence.files_unchanged, msg=str(record_evidence.changed_files))
        self.assertEqual(list(escaped.iterdir()), [], msg="the escape target received a write")
        self.assert_unchanged(before_ws, workspace)

    def test_f4_junction_kind_survives_missing_os_path_isjunction(self) -> None:
        """The junction probe must not depend on ``os.path.isjunction`` (3.12+).

        ``getattr(os.path, "isjunction", None)`` is the < 3.12 shape; the test
        emulates it by setting the attribute to ``None`` and requires the
        ``st_reparse_tag`` fallback to still identify the real NTFS junction.
        """
        linked = self.workspace("linked")
        external = self.workspace(
            "external", handoff_items=[handoff_item("alpha@example.org")],
        )
        shutil.rmtree(linked.data_dir)
        kind = make_directory_link(linked.data_dir, external.data_dir)
        if kind != "junction":
            self.skipTest(self.JUNCTION_SKIP)
        with mock.patch.object(os.path, "isjunction", None, create=True):
            self.assertEqual(
                link_kind(linked.data_dir), "junction",
                msg="the Python < 3.12 fallback did not identify the NTFS junction",
            )


# ---------------------------------------------------------------------------
# F6 — INBOX x pending matrix (synthetic SOP model)
# ---------------------------------------------------------------------------

class F6InboxPendingMatrixTest(G1RemainingCase):
    """F6: the four matrix cells and the two error cells, branch before execute."""

    def test_f6a_inbox_zero_and_pending_zero_is_a_noop(self) -> None:
        workspace = self.workspace(handoff_items=[])
        state = _pending_state(workspace)
        self.assertEqual(state["handoff_state"], "none")
        self.assertTrue(state["handoff_present"])
        decision = sop_branch(SopState(inbox_new=0, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "no_op")
        self.assertEqual(decision.gated_phases_present, ())
        self.assertNotIn("draft", decision.trace)
        self.assertEqual(self.ledger_lines(workspace), [])

    def test_f6b_inbox_zero_with_pending_runs_6f_and_never_executes(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        runner = run_case("F6", "INBOX 0 x pending > 0: consume 6f, no execute")
        state = _pending_state(workspace)
        self.assertEqual(state["pending_count"], 1)
        decision = sop_branch(SopState(inbox_new=0, draft_validated=False, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "consume_first_no_execute")
        self.assertIn("consume_pending_6f", decision.trace)
        self.assertNotIn("draft", decision.trace)
        self.assertEqual(decision.gated_phases_present, ())

        injected = InjectedLease()
        with injected:
            consumed = consume(
                workspace,
                workspace.evaluations([{
                    "message_id": "alpha@example.org", "outcome": "synthesized",
                    "vorgang_ref": "V-1", "evid_id": "E-1",
                }]),
                lease_id=injected.lease_id,
            )
        self.assertEqual(consumed.exit_code, 0, msg=json.dumps(consumed.envelope))
        self.assertEqual(consumed.data.get("appended"), 1)
        self.assertEqual(_pending_state(workspace)["pending_count"], 0)
        runner.finish(result=consumed)

    def test_f6c_inbox_without_pending_keeps_outbound_a_noop(self) -> None:
        workspace = self.workspace(handoff_items=[])
        state = _pending_state(workspace)
        decision = sop_branch(SopState(inbox_new=2, draft_validated=False, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "inbound_outbound_noop")
        self.assertIn("inbound_path", decision.trace)
        self.assertNotIn("draft", decision.trace)
        self.assertEqual(decision.gated_phases_present, ())
        self.assertEqual(self.ledger_lines(workspace), [], msg="the inbound path wrote a ledger line")

    def test_f6d_inbox_and_pending_stops_before_draft_and_syncs_again_after_6f(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        state = _pending_state(workspace)
        decision = sop_branch(SopState(inbox_new=1, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "stop_consume_first_then_resync")
        self.assertNotIn("draft", decision.trace)
        self.assertEqual(decision.gated_phases_present, ())

        stub = TransportStub(
            pages={SYNTH_DAY: [[synthetic_envelope("91")]]},
            details={"91": synthetic_detail("91", "sent-91@example.org")},
        )
        handoff_before = (workspace.data_dir / "new-sent-items.json").read_bytes()
        with stub:
            self.addCleanup(stub.uninstall)
            refused = run_sync_sent_mode(
                {"count": 10, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A, data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()
        self.assertFalse(refused["ok"], msg=json.dumps(refused)[:600])
        self.assertEqual(refused["reason_code"], "handoff_pending")
        self.assertEqual(refused["handoff"]["pending_count"], 1)
        self.assertEqual(
            (workspace.data_dir / "new-sent-items.json").read_bytes(), handoff_before,
            msg="the refused sync rewrote the pending handoff",
        )
        self.assertFalse((workspace.data_dir / "sent-index.jsonl").exists())
        self.assertEqual(self.ledger_lines(workspace), [])

        injected = InjectedLease()
        with injected:
            consumed = consume(
                workspace,
                workspace.evaluations([{
                    "message_id": "alpha@example.org", "outcome": "synthesized",
                    "vorgang_ref": "V-1", "evid_id": "E-1",
                }]),
                lease_id=injected.lease_id,
            )
        self.assertEqual(consumed.exit_code, 0, msg=json.dumps(consumed.envelope))
        self.assertEqual(consumed.data.get("appended"), 1)

        with stub:
            self.addCleanup(stub.uninstall)
            again = run_sync_sent_mode(
                {"count": 10, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A, data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()
        self.assertTrue(again["ok"], msg=json.dumps(again)[:600])
        self.assertEqual(again["new_count"], 1)
        self.assertEqual(again["handoff"]["state"], "written")

    def test_f6d_validated_draft_branches_immediately_and_never_executes(self) -> None:
        # The branch is taken right after the validated draft, not after execute.
        decision = sop_branch(SopState(
            inbox_new=1, handoff_state="none", handoff_present=True,
            pending_count=0, draft_validated=True, fresh_pending_after_draft=1,
        ))
        self.assertEqual(decision.decision, "stop_pending_after_draft")
        self.assertEqual(decision.index("draft_validated") + 1, decision.index("pending_gate_post_draft"))
        self.assertEqual(decision.gated_phases_present, ())
        self.assertLess(decision.index("pending_gate_post_draft"), decision.index("stop"))

    def test_f6e_invalid_handoff_is_neither_a_noop_nor_a_synthesis(self) -> None:
        workspace = self.workspace()
        handoff_path = workspace.data_dir / "new-sent-items.json"
        handoff_path.write_text("{not json", encoding="utf-8")
        corrupt_before = handoff_path.read_bytes()
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "handoff_invalid")

        decision = sop_branch(SopState(
            inbox_new=0, handoff_state="invalid", handoff_present=True, pending_count=None,
        ))
        self.assertEqual(decision.decision, "stop_handoff_invalid")
        self.assertEqual(decision.gated_phases_present, ())
        self.assertNotIn("no_op", decision.trace)

        stub = TransportStub(
            pages={SYNTH_DAY: [[synthetic_envelope("91")]]},
            details={"91": synthetic_detail("91", "sent-91@example.org")},
        )
        with stub:
            self.addCleanup(stub.uninstall)
            synced = run_sync_sent_mode(
                {"count": 10, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A, data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()
        self.assertFalse(synced["ok"], msg=json.dumps(synced)[:600])
        self.assertEqual(synced["reason_code"], "handoff_invalid")
        self.assertEqual(synced["handoff"]["state"], "invalid")
        self.assertEqual(handoff_path.read_bytes(), corrupt_before, msg="the handoff was consumed")
        self.assertFalse((workspace.data_dir / "sent-index.jsonl").exists())
        self.assertEqual(self.ledger_lines(workspace), [])

    def test_f6f_missing_handoff_is_no_noop_proof_but_allows_a_sync(self) -> None:
        workspace = self.workspace(handoff_missing=True)
        state = _pending_state(workspace)
        self.assertFalse(state["handoff_present"])
        self.assertEqual(state["pending_count"], 0)
        decision = sop_branch(SopState(inbox_new=0, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "sync_authorized_not_noop_proof")
        self.assertNotIn("no_op", decision.trace)

        loss = sop_branch(SopState(
            inbox_new=0, handoff_state="none", handoff_present=False,
            pending_count=0, documented_items=2,
        ))
        self.assertEqual(loss.decision, "stop_unexpected_loss")

        stub = TransportStub(
            pages={SYNTH_DAY: [[synthetic_envelope("91")]]},
            details={"91": synthetic_detail("91", "sent-91@example.org")},
        )
        with stub:
            self.addCleanup(stub.uninstall)
            synced = run_sync_sent_mode(
                {"count": 10, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A, data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()
        self.assertTrue(synced["ok"], msg=json.dumps(synced)[:600])
        self.assertEqual(synced["new_count"], 1)
        self.assertEqual(synced["handoff"]["state"], "written")
        self.assertEqual(_pending_state(workspace)["pending_count"], 1)


# ---------------------------------------------------------------------------
# F7 — pending before draft (existing and arising during the run)
# ---------------------------------------------------------------------------

class F7PendingBeforeDraftTest(G1RemainingCase):
    """F7: pending is consumed before any execute claim; exactly one ledger line."""

    def test_f7a_existing_pending_is_consumed_before_any_execute_claim(self) -> None:
        workspace = self.workspace(handoff_items=[handoff_item("alpha@example.org")])
        state = _pending_state(workspace)
        decision = sop_branch(SopState(inbox_new=0, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "consume_first_no_execute")
        self.assertNotIn("draft", decision.trace)
        self.assertEqual(decision.gated_phases_present, ())

        injected = InjectedLease()
        with injected:
            consumed = consume(
                workspace,
                workspace.evaluations([{
                    "message_id": "alpha@example.org", "outcome": "synthesized",
                    "vorgang_ref": "V-1", "evid_id": "E-1",
                }]),
                lease_id=injected.lease_id,
            )
        self.assertEqual(consumed.exit_code, 0, msg=json.dumps(consumed.envelope))
        lines = self.ledger_lines(workspace)
        self.assertEqual(len(lines), 1, msg="exactly one ledger line per consumed item")
        self.assertEqual(lines[0]["message_id"], "alpha@example.org")
        self.assertEqual(_pending_state(workspace)["pending_count"], 0)

        stub = TransportStub(pages={}, details={})
        with stub:
            self.addCleanup(stub.uninstall)
            resync = run_sync_sent_mode(
                {"count": 10, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A, data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()
        self.assertTrue(resync["ok"], msg=json.dumps(resync)[:600])
        self.assertEqual(resync["new_count"], 0)
        self.assertEqual(len(self.ledger_lines(workspace)), 1)

    def test_f7b_pending_created_during_the_run_is_gated_before_the_draft(self) -> None:
        workspace = self.workspace()
        stub = TransportStub(
            pages={SYNTH_DAY: [[synthetic_envelope("91")]]},
            details={"91": synthetic_detail("91", "sent-91@example.org")},
        )
        with stub:
            self.addCleanup(stub.uninstall)
            synced = run_sync_sent_mode(
                {"count": 10, "folder": SYNTH_FOLDER, "since": SYNTH_DAY},
                account=ACCOUNT_A, data_dir=workspace.data_dir,
            )
            stub.assert_no_real_transport()
        self.assertTrue(synced["ok"], msg=json.dumps(synced)[:600])
        self.assertEqual(synced["handoff"]["state"], "written")
        state = _pending_state(workspace)
        self.assertGreaterEqual(state["pending_count"], 1)

        # The pending item exists only after phase 1, but the pre-draft gate reads
        # the fresh handoff: no draft, review or execute while pending > 0.
        decision = sop_branch(SopState(inbox_new=0, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(decision.decision, "consume_first_no_execute")
        self.assertNotIn("draft", decision.trace)
        self.assertEqual(decision.gated_phases_present, ())

        # Even a draft that was already validated branches immediately when the
        # fresh view shows pending > 0 - never after execute.
        late = sop_branch(SopState(
            inbox_new=1, handoff_state="none", handoff_present=True, pending_count=0,
            draft_validated=True, fresh_pending_after_draft=state["pending_count"],
        ))
        self.assertEqual(late.decision, "stop_pending_after_draft")
        self.assertEqual(late.index("draft_validated") + 1, late.index("pending_gate_post_draft"))
        self.assertEqual(late.gated_phases_present, ())

        evaluations = workspace.evaluations([
            {
                "message_id": item["message_id"].strip("<>"), "outcome": "synthesized",
                "vorgang_ref": "V-1", "evid_id": "E-1",
            }
            for item in json.loads((workspace.data_dir / "new-sent-items.json").read_text("utf-8"))["items"]
        ])
        injected = InjectedLease()
        with injected:
            consumed = consume(workspace, evaluations, lease_id=injected.lease_id)
        self.assertEqual(consumed.exit_code, 0, msg=json.dumps(consumed.envelope))
        self.assertEqual(consumed.data.get("appended"), state["pending_count"])
        self.assertEqual(_pending_state(workspace)["pending_count"], 0)


# ---------------------------------------------------------------------------
# F8 — a second batch re-enters the pending/sync gate
# ---------------------------------------------------------------------------

class F8MultiBatchTest(G1RemainingCase):
    """F8: the second batch gates again instead of drafting over pending items."""

    def test_f8_second_batch_reenters_the_gate_before_drafting(self) -> None:
        workspace = self.workspace()
        stub = TransportStub(
            pages={
                SYNTH_DAY: [[synthetic_envelope("91")]],
                SYNTH_DAY_NEXT: [[synthetic_envelope("92")]],
            },
            details={
                "91": synthetic_detail("91", "sent-91@example.org"),
                "92": synthetic_detail("92", "sent-92@example.org"),
            },
        )
        config = {"count": 1, "folder": SYNTH_FOLDER, "since": SYNTH_DAY}
        with stub:
            self.addCleanup(stub.uninstall)
            first = run_sync_sent_mode(config, account=ACCOUNT_A, data_dir=workspace.data_dir)
            stub.assert_no_real_transport()
        self.assertTrue(first["ok"], msg=json.dumps(first)[:600])
        self.assertTrue(first["truncated"])
        self.assertEqual(first["new_count"], 1)
        self.assertEqual(first["handoff"]["state"], "written")

        batch_one = sop_branch(SopState(inbox_new=1, handoff_state="none", handoff_present=True, pending_count=0))
        self.assertEqual(batch_one.decision, "inbound_outbound_noop")

        state = _pending_state(workspace)
        self.assertEqual(state["pending_count"], 1)
        batch_two = sop_branch(SopState(inbox_new=1, **{
            key: state[key] for key in ("handoff_state", "handoff_present", "pending_count")
        }))
        self.assertEqual(batch_two.decision, "stop_consume_first_then_resync")
        self.assertNotIn("draft", batch_two.trace)
        self.assertEqual(batch_two.gated_phases_present, ())

        handoff_before = (workspace.data_dir / "new-sent-items.json").read_bytes()
        index_path = workspace.data_dir / "sent-index.jsonl"
        index_before = index_path.read_bytes() if index_path.exists() else None
        with stub:
            self.addCleanup(stub.uninstall)
            second = run_sync_sent_mode(config, account=ACCOUNT_A, data_dir=workspace.data_dir)
            stub.assert_no_real_transport()
        self.assertFalse(second["ok"], msg=json.dumps(second)[:600])
        self.assertEqual(second["reason_code"], "handoff_pending")
        self.assertEqual(second["handoff"]["pending_count"], 1)
        self.assertEqual(
            (workspace.data_dir / "new-sent-items.json").read_bytes(), handoff_before,
            msg="the second batch rewrote the pending handoff",
        )
        self.assertEqual(
            index_path.read_bytes() if index_path.exists() else None, index_before,
            msg="the refused second batch grew the sent-index",
        )
        self.assertEqual(self.ledger_lines(workspace), [])

        injected = InjectedLease()
        with injected:
            consumed = consume(
                workspace,
                workspace.evaluations([{
                    "message_id": "sent-91@example.org", "outcome": "synthesized",
                    "vorgang_ref": "V-1", "evid_id": "E-1",
                }]),
                lease_id=injected.lease_id,
            )
        self.assertEqual(consumed.exit_code, 0, msg=json.dumps(consumed.envelope))
        self.assertEqual(consumed.data.get("appended"), 1)

        with stub:
            self.addCleanup(stub.uninstall)
            third = run_sync_sent_mode(config, account=ACCOUNT_A, data_dir=workspace.data_dir)
            stub.assert_no_real_transport()
        self.assertTrue(third["ok"], msg=json.dumps(third)[:600])
        self.assertEqual(third["new_count"], 1)
        self.assertEqual(third["handoff"]["state"], "written")
        self.assertEqual(len(self.ledger_lines(workspace)), 1)


# ---------------------------------------------------------------------------
# F9 — durable evidence before the append
# ---------------------------------------------------------------------------

class F9DurableEvidenceTest(G1RemainingCase):
    """F9: without unique durable evidence there is no ledger line and no append."""

    MID = "beta@example.org"
    OTHER = "gamma@example.org"

    def _workspace(self) -> SyntheticWorkspace:
        return self.workspace(handoff_items=[handoff_item(self.MID)])

    def _assert_pending_and_unchanged(self, workspace, before) -> None:
        self.assertEqual(_pending_state(workspace)["pending_count"], 1, msg="the item left the handoff")
        self.assert_unchanged(before, workspace)

    def test_f9a_missing_evidence_file_stops_with_exit_two(self) -> None:
        workspace = self._workspace()
        before = workspace.snapshot()
        result = crash_check(
            workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=workspace.data_dir / "missing-durable-promotions.json",
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "promotion_recovery_incomplete")
        self.assertEqual(result.data.get("reason"), "evidence_unreadable")
        self._assert_pending_and_unchanged(workspace, before)

    def test_f9b_evid_not_in_the_file_stops_without_append(self) -> None:
        workspace = self._workspace()
        evidence = workspace.evidence([
            {"message_id": self.MID, "vorgang_ref": "V-2", "evid_id": "E-OTHER"},
        ])
        before = workspace.snapshot()
        result = crash_check(
            workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=evidence,
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "promotion_conflict")
        self._assert_pending_and_unchanged(workspace, before)

    def test_f9c_duplicate_evid_in_the_file_stops_without_append(self) -> None:
        workspace = self._workspace()
        evidence = workspace.evidence([
            {"message_id": self.MID, "vorgang_ref": "V-2", "evid_id": "E-2"},
            {"message_id": self.OTHER, "vorgang_ref": "V-3", "evid_id": "E-2"},
        ])
        before = workspace.snapshot()
        result = crash_check(
            workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=evidence,
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "evid_shared_conflict")
        self._assert_pending_and_unchanged(workspace, before)

    def test_f9d_evid_bound_to_another_message_stops_without_append(self) -> None:
        workspace = self._workspace()
        evidence = workspace.evidence([
            {"message_id": self.OTHER, "vorgang_ref": "V-3", "evid_id": "E-2"},
        ])
        before = workspace.snapshot()
        result = crash_check(
            workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=evidence,
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "evid_shared_conflict")
        self._assert_pending_and_unchanged(workspace, before)

    def test_f9e_durable_promotion_without_a_ledger_line_goes_to_recovery(self) -> None:
        workspace = self._workspace()
        evidence = workspace.evidence([
            {"message_id": self.MID, "vorgang_ref": "V-2", "evid_id": "E-2"},
        ])
        evidence_before = evidence.read_bytes()
        crash = crash_check(
            workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=evidence,
        )
        self.assertEqual(crash.exit_code, 0, msg=json.dumps(crash.envelope))
        self.assertEqual(crash.data.get("status"), "unique")
        self.assertTrue(crash.data.get("safe_to_append"))
        before = workspace.snapshot()

        injected = InjectedLease()
        with injected:
            recovered = recover_promotion(
                workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
                evidence_file=evidence, lease_id=injected.lease_id,
            )
        self.assertEqual(recovered.exit_code, 0, msg=json.dumps(recovered.envelope))
        self.assertEqual(recovered.data.get("status"), "recovered")
        self.assertEqual(recovered.data.get("appended"), 1)
        self.assertEqual(len(self.ledger_lines(workspace)), 1)
        self.assertEqual(evidence.read_bytes(), evidence_before, msg="a promotion was repeated")
        self.assertEqual(
            self.changed_files(before, workspace), [LEDGER_REL],
            msg="the recovery wrote more than the one missing ledger line",
        )

    def test_f9e_no_append_without_a_uniqueness_proof(self) -> None:
        workspace = self._workspace()
        evidence = workspace.evidence([
            {"message_id": self.MID, "vorgang_ref": "V-2", "evid_id": "E-2"},
            {"message_id": self.MID, "vorgang_ref": "V-2", "evid_id": "E-2"},
        ])
        before = workspace.snapshot()
        crash = crash_check(
            workspace, message_id=self.MID, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=evidence,
        )
        self.assertEqual(crash.exit_code, 2, msg=json.dumps(crash.envelope))
        self.assertEqual(crash.reason_code, "promotion_ambiguous")
        self.assertFalse(crash.data.get("safe_to_append", True))
        self._assert_pending_and_unchanged(workspace, before)


# ---------------------------------------------------------------------------
# F10 — anchor consistency at both time points
# ---------------------------------------------------------------------------

class F10AnchorConsistencyTest(G1RemainingCase):
    """F10: anchor conflicts stop the append; the promotion decision stays partial.

    Adapter seam only.  The bundle exposes no consumer promotion writer, so the
    consumer promotion *decision* is not observable here: F10(a) proves the
    append-side refusal (no ledger line, no bundle-written promotion file) and is
    reported as partial.  F10(b) proves the append-side conflict time point at the
    declared ``recover-promotion`` seam: the durable promotion record exists
    (synthetic fixture state), ``crash-check`` authorizes the append, the ledger
    anchor then changes, and the recovery append is refused while the promotion
    stays byte-identical.  The promotion record's origin is still fixture state,
    not an observed consumer decision.
    """

    ROOT = "alpha@example.org"
    REPLY = "beta@example.org"

    def _workspace(self, ledger_lines, *, evidence=None) -> SyntheticWorkspace:
        workspace = self.workspace(
            handoff_items=[handoff_item(self.REPLY, in_reply_to=self.ROOT)],
            ledger_lines=ledger_lines,
        )
        if evidence is not None:
            workspace.evidence(evidence)
        return workspace

    def test_f10a_conflict_before_the_promotion_stops_without_append(self) -> None:
        workspace = self._workspace([
            ledger_line(self.ROOT, vorgang_ref="V-1", evid_id="E-1"),
        ])
        before = workspace.snapshot()
        injected = InjectedLease()
        evidence = run_case(
            "F10", "an anchor conflict before the promotion stops before any append",
            lease=injected,
            details={
                "time_point": "before_promotion",
                "promotion_decision_seam": "absent",
                "observed": "adapter_refusal_only",
                "partial": True,
            },
        )
        with injected:
            result = record(
                workspace, "--account", ACCOUNT_A, "--outcome", "synthesized",
                "--vorgang-ref", "V-2", "--evid-id", "E-2",
                message_id=self.REPLY, lease_id=injected.lease_id,
            )
        record_evidence = evidence.finish(
            result=result, snapshot_before=before, snapshot_after=workspace.snapshot(),
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "anchor_conflict")
        self.assertEqual(result.data.get("conflicting_refs"), ["V-1"])
        self.assertTrue(record_evidence.files_unchanged, msg=str(record_evidence.changed_files))
        self.assertEqual(len(self.ledger_lines(workspace)), 1, msg="the ancestor line changed")
        self.assertEqual(_pending_state(workspace)["pending_count"], 1)
        self.assertFalse(
            (workspace.data_dir / "durable-promotions.json").exists(),
            msg="the refused path wrote a durable promotion although the seam is consumer-side",
        )

    def test_f10b_conflict_between_promotion_and_append_refuses_the_append(self) -> None:
        # Two observable points at the adapter seam:
        #   t0: the durable promotion record exists (a synthetic fixture record,
        #       not a consumer decision) and crash-check authorizes the append
        #       (unique, append required, safe_to_append true);
        #   t1: the ledger anchor changes after that authorization but before the
        #       append; ``recover-promotion`` - the declared append-after-
        #       promotion seam - refuses and writes nothing.
        workspace = self.workspace(
            handoff_items=[handoff_item(self.REPLY, in_reply_to=self.ROOT)],
        )
        evidence_file = workspace.evidence([
            {"message_id": self.REPLY, "vorgang_ref": "V-2", "evid_id": "E-2"},
        ])
        evidence_before = evidence_file.read_bytes()

        t0 = crash_check(
            workspace, message_id=self.REPLY, vorgang_ref="V-2", evid_id="E-2",
            evidence_file=evidence_file,
        )
        self.assertEqual(t0.exit_code, 0, msg=json.dumps(t0.envelope))
        self.assertEqual(t0.data.get("status"), "unique")
        self.assertTrue(t0.data.get("append_required"))
        self.assertTrue(t0.data.get("safe_to_append"))
        self.assertEqual(self.ledger_lines(workspace), [], msg="t0 already had a ledger line")

        # The anchor changes *between* the authorized promotion and the append.
        workspace.write_ledger([ledger_line(self.ROOT, vorgang_ref="V-1", evid_id="E-1")])
        before = workspace.snapshot()
        injected = InjectedLease()
        evidence = run_case(
            "F10", "a conflict between promotion and append refuses the recovery append",
            lease=injected,
            details={
                "time_point": "between_promotion_and_append",
                "promotion_record_source": "synthetic_fixture",
                "consumer_decision_seam": "absent",
                "append_seam": "recover-promotion",
                "phases": [
                    {"phase": "promotion_durable", "ledger_lines": 0,
                     "crash_status": t0.data.get("status"),
                     "append_required": t0.data.get("append_required")},
                    {"phase": "anchor_changed", "ledger_lines": 1,
                     "ancestor_vorgang": "V-1"},
                ],
            },
        )
        with injected:
            result = recover_promotion(
                workspace, message_id=self.REPLY, vorgang_ref="V-2", evid_id="E-2",
                evidence_file=evidence_file, lease_id=injected.lease_id,
            )
        record_evidence = evidence.finish(
            result=result, snapshot_before=before, snapshot_after=workspace.snapshot(),
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "promotion_anchor_conflict")
        self.assertEqual(result.data.get("conflicting_refs"), ["V-1"])
        self.assertEqual(result.data.get("state"), "conflict")
        self.assertTrue(record_evidence.files_unchanged, msg=str(record_evidence.changed_files))
        self.assertEqual(evidence_file.read_bytes(), evidence_before, msg="the promotion changed")
        self.assertEqual(len(self.ledger_lines(workspace)), 1)
        self.assertEqual(_pending_state(workspace)["pending_count"], 1)

    def test_f10c_contradictory_ancestor_lines_stop_as_anchor_ambiguous(self) -> None:
        workspace = self._workspace([
            ledger_line(self.ROOT, vorgang_ref="V-1", evid_id="E-1"),
            ledger_line(self.ROOT, vorgang_ref="V-9", evid_id="E-9"),
        ])
        before = workspace.snapshot()
        injected = InjectedLease()
        with injected:
            result = record(
                workspace, "--account", ACCOUNT_A, "--outcome", "synthesized",
                "--vorgang-ref", "V-2", "--evid-id", "E-2",
                message_id=self.REPLY, lease_id=injected.lease_id,
            )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "anchor_ambiguous")
        self.assertEqual(len(result.data.get("ambiguous_refs") or []), 1)
        self.assert_unchanged(before, workspace)
        self.assertEqual(len(self.ledger_lines(workspace)), 2)
        self.assertEqual(_pending_state(workspace)["pending_count"], 1)


# ---------------------------------------------------------------------------
# F15 — no personal metadata in adapter output
# ---------------------------------------------------------------------------

class F15NoPersonalMetadataTest(G1RemainingCase):
    """F15: counters and truncated hash references only; the transit file stays as is."""

    ITEM_KEYS = {
        "item_ref", "thread_ref", "headers_complete", "truncated_fields",
        "anchored", "anchor", "waits_for", "waits_for_count",
    }
    FORBIDDEN = (
        "Synthetic subject",
        "sender@example.org",
        "recipient@example.org",
        "Mon, 02 Nov 2026",
        "9001",
        "alpha@example.org",
        "beta@example.org",
    )

    def test_f15_pending_output_carries_no_subject_address_or_raw_id(self) -> None:
        workspace = self.workspace(handoff_items=[
            handoff_item("alpha@example.org"),
            handoff_item("beta@example.org"),
        ])
        before = workspace.snapshot()
        result = pending(workspace, "--account", ACCOUNT_A)
        self.assertEqual(result.exit_code, 0, msg=json.dumps(result.envelope))
        serialized = json.dumps(result.envelope)
        for fragment in self.FORBIDDEN:
            self.assertNotIn(fragment, serialized, msg=f"the output leaked {fragment!r}")
        self.assertEqual(result.data.get("pending_count"), 2)
        self.assertIsInstance(result.data.get("consumed_ignored"), int)
        for item in result.data.get("items") or []:
            self.assertEqual(set(item), self.ITEM_KEYS)
            self.assertRegex(item["item_ref"], r"^sha256:[0-9a-f]{16}$")
        refs = [item["item_ref"] for item in result.data["items"]]
        self.assertEqual(refs, [item_ref("alpha@example.org"), item_ref("beta@example.org")])
        self.assert_unchanged(before, workspace)

        # The exclusion applies to the adapter output, not to the transit handoff.
        transit = (workspace.data_dir / "new-sent-items.json").read_text("utf-8")
        self.assertIn("Synthetic subject", transit)
        self.assertIn("sender@example.org", transit)


# ---------------------------------------------------------------------------
# F16 — reference-less anchor
# ---------------------------------------------------------------------------

class F16ReferenceLessAnchorTest(G1RemainingCase):
    """F16: an anchor without a Vorgang stops with ``reference_anchor_unresolved``."""

    ROOT = "alpha@example.org"
    REPLY = "beta@example.org"

    def test_f16_reference_less_ancestor_stops_without_append(self) -> None:
        workspace = self.workspace(
            handoff_items=[handoff_item(self.REPLY, in_reply_to=self.ROOT)],
            ledger_lines=[ledger_line(self.ROOT, vorgang_ref=None, evid_id="E-1")],
        )
        before = workspace.snapshot()
        injected = InjectedLease()
        evidence = run_case(
            "F16", "a resolved anchor without a Vorgang stops before the append", lease=injected,
        )
        with injected:
            result = record(
                workspace, "--account", ACCOUNT_A, "--outcome", "synthesized",
                "--vorgang-ref", "V-9", "--evid-id", "E-9",
                message_id=self.REPLY, lease_id=injected.lease_id,
            )
        record_evidence = evidence.finish(
            result=result, snapshot_before=before, snapshot_after=workspace.snapshot(),
        )
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertEqual(result.reason_code, "reference_anchor_unresolved")
        self.assertRegex(str(result.data.get("anchor_ref")), r"^sha256:[0-9a-f]{16}$")
        self.assertTrue(record_evidence.files_unchanged, msg=str(record_evidence.changed_files))
        self.assertEqual(len(self.ledger_lines(workspace)), 1, msg="a Vorgang was derived")
        self.assertIsNone(self.ledger_lines(workspace)[0]["vorgang_ref"])
        self.assertEqual(_pending_state(workspace)["pending_count"], 1)


# ---------------------------------------------------------------------------
# F18 — crash recovery variants (a)-(l), strictly separated
# ---------------------------------------------------------------------------

class F18CrashRecoveryTest(G1RemainingCase):
    """F18: an append needs proof, ``unique`` and ``safe_to_append``; a no-op needs none."""

    MID = "beta@example.org"
    OTHER = "gamma@example.org"
    ANCESTOR = "alpha@example.org"
    REF = "V-2"
    EVID = "E-2"

    def _workspace(
        self,
        *,
        handoff_items: list[dict] | None = None,
        ledger_lines: list[dict] | None = None,
        evidence: list[dict] | None = None,
    ) -> tuple[SyntheticWorkspace, Path | None]:
        workspace = self.workspace(handoff_items=handoff_items, ledger_lines=ledger_lines)
        path = workspace.evidence(evidence) if evidence is not None else None
        return workspace, path

    def _evaluate(
        self,
        workspace: SyntheticWorkspace,
        evidence_file: Path | None,
        *,
        completeness_proven: bool = True,
        refusal_probe: bool = False,
    ) -> dict:
        """Run the document gate: crash-check, consumer decision, recovery only if authorized."""
        before = workspace.snapshot()
        crash = crash_check(
            workspace, message_id=self.MID, vorgang_ref=self.REF, evid_id=self.EVID,
            evidence_file=evidence_file,
        )
        status = crash.data.get("status")
        ledger_state = crash.data.get("ledger_state")
        # Pass the adapter fields through uncoerced: a missing or mistyped field
        # must be refused by the gate, never silently converted by bool().
        append_required = crash.data.get("append_required")
        safe_to_append = crash.data.get("safe_to_append")
        decision, unmet = consumer_recovery_gate(
            completeness_proven=completeness_proven,
            status=status,
            ledger_state=ledger_state,
            safe_to_append=safe_to_append,
            append_required=append_required,
            exit_code=crash.exit_code,
        )
        gate_open = decision == "recovery"
        consumer_noop = None
        if decision == "consumer_noop":
            # Accepted F18(h) exception: no append is required, so the consumer
            # reports the synthetic no-op result instead of calling recovery.
            consumer_noop = {
                "exit_code": 0,
                "status": "already_consumed",
                "appended": 0,
                "recovery_called": False,
                "synthetic": True,
                "evidence_class": "synthetic_consumer_noop_model",
                "observed_adapter_envelope": False,
                "basis": {
                    "ledger_state": crash.data.get("ledger_state"),
                    "append_required": append_required,
                    "safe_to_append": safe_to_append,
                },
            }
        gate = {
            "completeness_proven": completeness_proven,
            "status": status,
            "ledger_state": ledger_state,
            "append_required": append_required,
            "safe_to_append": safe_to_append,
            "requirements": list(CONSUMER_RECOVERY_GATE),
            "unmet": list(unmet),
            "decision": decision,
            "open": gate_open,
        }
        recovery = None
        probe = None
        lease_source = "none"
        if gate_open:
            injected = InjectedLease()
            with injected:
                recovery = recover_promotion(
                    workspace, message_id=self.MID, vorgang_ref=self.REF, evid_id=self.EVID,
                    evidence_file=evidence_file, lease_id=injected.lease_id,
                )
            lease_source = injected.source
        elif refusal_probe:
            # Adapter-boundary probe only: the gate above already forbade a recovery;
            # this call must be refused without any write (it is not a recovery).
            injected = InjectedLease()
            with injected:
                probe = recover_promotion(
                    workspace, message_id=self.MID, vorgang_ref=self.REF, evid_id=self.EVID,
                    evidence_file=evidence_file, lease_id=injected.lease_id,
                )
            lease_source = injected.source
        after = workspace.snapshot()
        return {
            "crash": crash,
            "recovery": recovery,
            "probe": probe,
            "gate": gate,
            "gate_open": gate_open,
            "consumer_noop": consumer_noop,
            "completeness_proven": completeness_proven,
            "lease_source": lease_source,
            "before": before,
            "after": after,
            "ledger_lines": self.ledger_lines(workspace),
        }

    def _assert_no_write(self, outcome: dict) -> None:
        self.assertEqual(outcome["before"], outcome["after"], msg="a refused path wrote")

    def test_f18a_unique_complete_evidence_appends_exactly_one_line(self) -> None:
        workspace, evidence = self._workspace(evidence=[
            {"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID},
        ])
        outcome = self._evaluate(workspace, evidence)
        self.assertTrue(outcome["gate_open"], msg=json.dumps(outcome["crash"].envelope))
        self.assertEqual(outcome["gate"]["unmet"], [])
        self.assertEqual(outcome["crash"].data.get("status"), "unique")
        self.assertTrue(outcome["crash"].data.get("safe_to_append"))
        recovered = outcome["recovery"]
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.exit_code, 0, msg=json.dumps(recovered.envelope))
        self.assertTrue(recovered.success)
        self.assertEqual(recovered.data.get("status"), "recovered")
        self.assertEqual(recovered.data.get("appended"), 1)
        self.assertEqual(len(outcome["ledger_lines"]), 1)
        self.assertEqual(
            sorted(
                name for name in set(outcome["before"]) | set(outcome["after"])
                if outcome["before"].get(name) != outcome["after"].get(name)
            ),
            [LEDGER_REL],
        )

    def test_f18b_unproven_completeness_forbids_the_mutating_recovery(self) -> None:
        workspace, evidence = self._workspace(evidence=[
            {"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID},
        ])
        outcome = self._evaluate(workspace, evidence, completeness_proven=False)
        self.assertFalse(outcome["gate_open"], msg="an unproven list authorized a recovery")
        self.assertEqual(outcome["gate"]["unmet"], ["completeness_proven"])
        self.assertIsNone(outcome["recovery"], msg="the mutating recovery was called (NO-GO)")
        self.assertIsNone(outcome["probe"])
        self.assertEqual(outcome["crash"].data.get("evidence_matches"), 1)
        # The adapter cannot attest list completeness: the proof is consumer-side.
        self.assertNotIn("completeness_proven", outcome["crash"].data)
        self._assert_no_write(outcome)

    def test_f18c_adapter_refusal_probe_is_partial_not_a_consumer_flow(self) -> None:
        """F18(c) partial: only the adapter refusal is observed.

        The consumer gate is already closed by the ``absent`` classification, so
        the ``recover-promotion`` call is an adapter-boundary probe, not a consumer
        recovery flow.  It proves the refusal reason, the exit code and the absence
        of a write; it does not prove a complete consumer path.
        """
        for evidence_payload in (
            [],
            [{"message_id": self.OTHER, "vorgang_ref": "V-3", "evid_id": "E-3"}],
        ):
            with self.subTest(records=len(evidence_payload)):
                workspace, evidence = self._workspace(evidence=evidence_payload)
                outcome = self._evaluate(workspace, evidence, refusal_probe=True)
                self.assertFalse(outcome["gate_open"])
                self.assertEqual(outcome["gate"]["decision"], "refuse")
                self.assertIsNone(outcome["recovery"])
                crash = outcome["crash"]
                self.assertEqual(crash.exit_code, 0, msg=json.dumps(crash.envelope))
                self.assertEqual(crash.data.get("status"), "absent")
                self.assertEqual(crash.data.get("evidence_state"), "absent")
                self.assertFalse(crash.data.get("safe_to_append"))
                # ``absent`` is a read-only classification: the adapter reports the
                # reason code on the refused mutating path, not on the classification.
                self.assertIsNone(crash.reason_code)
                probe = outcome["probe"]
                self.assertIsNotNone(probe, msg="the adapter refusal was not exercised")
                self.assertEqual(probe.exit_code, 2, msg=json.dumps(probe.envelope))
                self.assertEqual(probe.reason_code, "promotion_not_found")
                self._assert_no_write(outcome)

    def test_f18d_differing_triad_pair_is_promotion_conflict(self) -> None:
        workspace, evidence = self._workspace(evidence=[
            {"message_id": self.MID, "vorgang_ref": "V-OTHER", "evid_id": self.EVID},
        ])
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "promotion_conflict")
        self._assert_no_write(outcome)

    def test_f18e_multiple_matching_records_are_promotion_ambiguous(self) -> None:
        workspace, evidence = self._workspace(evidence=[
            {"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID},
            {"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID},
        ])
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "promotion_ambiguous")
        self._assert_no_write(outcome)

    def test_f18f_foreign_evid_binding_is_evid_shared_conflict(self) -> None:
        workspace, evidence = self._workspace(evidence=[
            {"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID},
            {"message_id": self.OTHER, "vorgang_ref": "V-3", "evid_id": self.EVID},
        ])
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "evid_shared_conflict")
        self._assert_no_write(outcome)

    def test_f18g_conflicting_ledger_line_is_ledger_conflict(self) -> None:
        workspace, evidence = self._workspace(
            ledger_lines=[ledger_line(self.MID, vorgang_ref="V-9", evid_id="E-9")],
            evidence=[{"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID}],
        )
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "ledger_conflict")
        self._assert_no_write(outcome)

    def test_f18h_already_consumed_is_a_synthetic_consumer_noop_without_recovery(self) -> None:
        """(h) synthetic model: ``already_consumed`` without an append is exit 0.

        Accepted contract exception (TESTCORR-003): the consumer gate requires
        ``unique`` **and** ``safe_to_append: true`` only when an append is required.
        The item is already consumed, so the consumer model reports a synthetic
        no-op result with exit 0 and never calls ``recover-promotion``; the
        adapter's own ``already_consumed`` envelope therefore stays unobserved.  The
        literal below is labelled ``synthetic_consumer_noop_model`` and
        ``observed_adapter_envelope: false``; it is not presented as observed
        production consumer output.  ``safe_to_append: false`` is explicitly not a
        trigger here.
        """
        workspace, evidence = self._workspace(
            ledger_lines=[ledger_line(self.MID, vorgang_ref=self.REF, evid_id=self.EVID)],
            evidence=[{"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID}],
        )
        outcome = self._evaluate(workspace, evidence)
        crash = outcome["crash"]
        self.assertEqual(crash.exit_code, 0, msg=json.dumps(crash.envelope))
        self.assertEqual(crash.data.get("status"), "unique")
        self.assertEqual(crash.data.get("ledger_state"), "unique")
        self.assertFalse(crash.data.get("append_required"))
        self.assertFalse(crash.data.get("safe_to_append"))
        self.assertEqual(
            outcome["gate"],
            {
                "completeness_proven": True,
                "status": "unique",
                "ledger_state": "unique",
                "append_required": False,
                "safe_to_append": False,
                "requirements": ["completeness_proven", "unique", "safe_to_append"],
                "unmet": [],
                "decision": "consumer_noop",
                "open": False,
            },
        )
        self.assertFalse(outcome["gate_open"], msg="a no-op opened the append gate")
        self.assertIsNone(
            outcome["recovery"], msg="recovery ran for an already-consumed item",
        )
        self.assertEqual(
            outcome["consumer_noop"],
            {
                "exit_code": 0,
                "status": "already_consumed",
                "appended": 0,
                "recovery_called": False,
                "synthetic": True,
                "evidence_class": "synthetic_consumer_noop_model",
                "observed_adapter_envelope": False,
                "basis": {
                    "ledger_state": "unique",
                    "append_required": False,
                    "safe_to_append": False,
                },
            },
        )
        self.assertFalse(
            outcome["consumer_noop"]["observed_adapter_envelope"],
            msg="the synthetic no-op was presented as an observed adapter envelope",
        )
        self._assert_no_write(outcome)
        self.assertEqual(len(outcome["ledger_lines"]), 1)

    def test_f18i_foreign_evid_without_a_target_record_is_not_absent(self) -> None:
        workspace, evidence = self._workspace(evidence=[
            {"message_id": self.OTHER, "vorgang_ref": "V-3", "evid_id": self.EVID},
        ])
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2, msg=json.dumps(outcome["crash"].envelope))
        self.assertEqual(outcome["crash"].reason_code, "evid_shared_conflict")
        self.assertNotEqual(outcome["crash"].data.get("status"), "absent")
        self._assert_no_write(outcome)

    def test_f18j_contradictory_ancestor_lines_are_promotion_anchor_ambiguous(self) -> None:
        workspace, evidence = self._workspace(
            handoff_items=[handoff_item(self.MID, in_reply_to=self.ANCESTOR)],
            ledger_lines=[
                ledger_line(self.ANCESTOR, vorgang_ref="V-1", evid_id="E-1"),
                ledger_line(self.ANCESTOR, vorgang_ref="V-9", evid_id="E-9"),
            ],
            evidence=[{"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID}],
        )
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "promotion_anchor_ambiguous")
        self._assert_no_write(outcome)

    def test_f18j_another_ancestor_vorgang_is_promotion_anchor_conflict(self) -> None:
        workspace, evidence = self._workspace(
            handoff_items=[handoff_item(self.MID, in_reply_to=self.ANCESTOR)],
            ledger_lines=[ledger_line(self.ANCESTOR, vorgang_ref="V-1", evid_id="E-1")],
            evidence=[{"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID}],
        )
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "promotion_anchor_conflict")
        self._assert_no_write(outcome)

    def test_f18k_resolved_ancestor_without_a_vorgang_is_unresolved(self) -> None:
        workspace, evidence = self._workspace(
            handoff_items=[handoff_item(self.MID, in_reply_to=self.ANCESTOR)],
            ledger_lines=[ledger_line(self.ANCESTOR, vorgang_ref=None, evid_id="E-1")],
            evidence=[{"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID}],
        )
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "promotion_anchor_unresolved")
        self._assert_no_write(outcome)

    def test_f18k_incomplete_headers_are_handoff_metadata_incomplete(self) -> None:
        workspace, evidence = self._workspace(
            handoff_items=[handoff_item(self.MID, headers_complete=False)],
            evidence=[{"message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID}],
        )
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "handoff_metadata_incomplete")
        self._assert_no_write(outcome)

    def test_f18l_contradicting_handoff_and_evidence_metadata_conflict(self) -> None:
        workspace, evidence = self._workspace(
            handoff_items=[handoff_item(self.MID, in_reply_to=self.ANCESTOR)],
            evidence=[{
                "message_id": self.MID, "vorgang_ref": self.REF, "evid_id": self.EVID,
                "in_reply_to": f"<{self.OTHER}>",
            }],
        )
        outcome = self._evaluate(workspace, evidence)
        self.assertFalse(outcome["gate_open"])
        self.assertEqual(outcome["crash"].exit_code, 2)
        self.assertEqual(outcome["crash"].reason_code, "promotion_metadata_conflict")
        self._assert_no_write(outcome)


# ---------------------------------------------------------------------------
# Gate A — direct condition table (consumer model, not production code)
# ---------------------------------------------------------------------------

class ConsumerRecoveryGateTableTest(unittest.TestCase):
    """Gate A, condition by condition: each unmet entry alone forbids an append.

    This is a direct table over the declared test-side gate model.  It isolates
    the discriminating negative conditions for a *required* append; it does not
    claim a production consumer implementation.
    """

    def test_rev019_missing_append_required_cannot_become_a_consumer_noop(self) -> None:
        # REV-019: bool(None) must not turn a missing adapter field into False.
        base = dict(
            completeness_proven=True, status="unique", ledger_state="unique",
            safe_to_append=False, exit_code=0,
        )
        self.assertEqual(
            consumer_recovery_gate(append_required=None, **base),
            ("refuse", CONSUMER_RECOVERY_GATE),
        )
        self.assertEqual(
            consumer_recovery_gate(**base),
            ("refuse", CONSUMER_RECOVERY_GATE),
            msg="an omitted append_required was accepted as a no-op",
        )
        self.assertEqual(
            consumer_recovery_gate(append_required=False, **base),
            ("consumer_noop", ()),
        )

    def test_noop_requires_a_proven_unique_ledger_state(self) -> None:
        base = dict(
            completeness_proven=True, status="unique", safe_to_append=False,
            append_required=False, exit_code=0,
        )
        self.assertEqual(
            consumer_recovery_gate(ledger_state="unique", **base),
            ("consumer_noop", ()),
        )
        for ledger_state in (None, "absent", "conflict"):
            with self.subTest(ledger_state=ledger_state):
                decision, _unmet = consumer_recovery_gate(
                    ledger_state=ledger_state, **base,
                )
                self.assertEqual(
                    decision, "refuse",
                    msg=f"ledger_state {ledger_state!r} reached a consumer no-op",
                )

    def test_gate_problems_name_missing_mistyped_and_contradictory_fields(self) -> None:
        base = dict(
            completeness_proven=True, status="unique", ledger_state="absent",
            safe_to_append=True, append_required=True, exit_code=0,
        )
        self.assertEqual(consumer_gate_problems(**base), ())
        cases = (
            ("completeness_proven missing",
             {key: value for key, value in base.items() if key != "completeness_proven"},
             "completeness_proven:missing"),
            ("completeness_proven None", {**base, "completeness_proven": None},
             "completeness_proven:type"),
            ("append_required missing",
             {key: value for key, value in base.items() if key != "append_required"},
             "append_required:missing"),
            ("append_required None", {**base, "append_required": None},
             "append_required:type"),
            ("append_required string", {**base, "append_required": "false"},
             "append_required:type"),
            ("safe_to_append None", {**base, "safe_to_append": None},
             "safe_to_append:type"),
            ("ledger_state missing",
             {key: value for key, value in base.items() if key != "ledger_state"},
             "ledger_state:missing"),
            ("ledger_state None", {**base, "ledger_state": None},
             "ledger_state:type"),
            ("ledger_state unknown", {**base, "ledger_state": "unknown"},
             "ledger_state:value"),
            ("status non-string", {**base, "status": 1}, "status:type"),
            ("status unknown", {**base, "status": "unknown"}, "status:value"),
            ("exit_code bool", {**base, "exit_code": True}, "exit_code:type"),
            ("status conflict with append", {**base, "status": "conflict"},
             "status:contradiction"),
            ("ledger unique with append", {**base, "ledger_state": "unique"},
             "ledger_state:contradiction"),
            ("safe true without append", {**base, "append_required": False},
             "safe_to_append:contradiction"),
            ("append true with exit 2", {**base, "exit_code": 2},
             "exit_code:contradiction"),
            ("append false with unique and absent",
             {**base, "append_required": False, "safe_to_append": False},
             "append_required:contradiction"),
        )
        for label, inputs, expected in cases:
            with self.subTest(case=label):
                self.assertIn(expected, consumer_gate_problems(**inputs), msg=label)
                self.assertEqual(
                    consumer_recovery_gate(**inputs)[0], "refuse", msg=label,
                )

    def test_gate_a_requires_every_condition_when_an_append_is_required(self) -> None:
        cases = (
            ("all satisfied", dict(
                completeness_proven=True, status="unique", ledger_state="absent",
                safe_to_append=True, append_required=True, exit_code=0,
            ), "recovery", ()),
            ("safe_to_append false", dict(
                completeness_proven=True, status="unique", ledger_state="absent",
                safe_to_append=False, append_required=True, exit_code=0,
            ), "refuse", ("safe_to_append",)),
            ("completeness unproven", dict(
                completeness_proven=False, status="unique", ledger_state="absent",
                safe_to_append=True, append_required=True, exit_code=0,
            ), "refuse", ("completeness_proven",)),
            ("coherent non-zero crash exit", dict(
                completeness_proven=True, status="absent", ledger_state="absent",
                safe_to_append=False, append_required=False, exit_code=2,
            ), "refuse", ("unique", "safe_to_append")),
        )
        for label, inputs, decision, unmet in cases:
            with self.subTest(condition=label):
                self.assertEqual(
                    consumer_recovery_gate(**inputs), (decision, unmet),
                    msg=f"gate A did not discriminate {label!r}",
                )

    def test_only_already_consumed_without_append_is_a_consumer_noop(self) -> None:
        self.assertEqual(
            consumer_recovery_gate(
                completeness_proven=True, status="unique", ledger_state="unique",
                safe_to_append=False, append_required=False, exit_code=0,
            ),
            ("consumer_noop", ()),
        )
        # Without an append, ``safe_to_append`` is not consulted, but a
        # non-``unique`` classification still refuses: only the accepted
        # already-consumed case is a no-op.
        self.assertEqual(
            consumer_recovery_gate(
                completeness_proven=True, status="conflict", ledger_state="absent",
                safe_to_append=False, append_required=False, exit_code=0,
            ),
            ("refuse", ("unique", "safe_to_append")),
        )


# ---------------------------------------------------------------------------
# F20 — legacy inventory and import
# ---------------------------------------------------------------------------

def inventory_record(
    mid: str,
    *,
    status: str = "synthesized",
    vorgang_ref: str | None = "V-1",
    evid_id: str | None = "E-1",
    reason_code: str | None = None,
    recorded_at: str = "2026-11-01T09:00:00Z",
) -> dict:
    """One schema-shaped synthetic legacy-inventory record."""
    record: dict = {
        "schema_version": 1,
        "message_id": f"<{mid}>",
        "status": status,
        "recorded_at": recorded_at,
    }
    if vorgang_ref is not None:
        record["vorgang_ref"] = vorgang_ref
    if evid_id is not None:
        record["evid_id"] = evid_id
    if reason_code is not None:
        record["reason_code"] = reason_code
    return record


class F20LegacyImportTest(G1RemainingCase):
    """F20: blocked plans write nothing; a valid apply appends only missing lines."""

    ALPHA = "alpha@example.org"
    BETA = "beta@example.org"
    GAMMA = "gamma@example.org"

    def _inventory(self, workspace: SyntheticWorkspace, records: list[dict]) -> Path:
        path = workspace.root / "legacy-inventory.jsonl"
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
        )
        return path

    def _assert_blocked(self, workspace: SyntheticWorkspace, result, code: str) -> None:
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertTrue(result.success, msg="the documented blocked-plan Exception keeps success")
        self.assertFalse(result.data.get("ok"))
        self.assertTrue(result.data.get("blocked"))
        self.assertIsNone((result.data.get("approval") or {}).get("token"))
        self.assertIn(code, {conflict.get("code") for conflict in result.data["conflicts"]})
        assert_no_host_paths(result.data)

    def test_f20a_synthesized_without_evid_blocks_the_plan(self) -> None:
        workspace = self.workspace()
        inventory = self._inventory(workspace, [
            inventory_record(self.ALPHA, evid_id=None),
        ])
        before = workspace.snapshot()
        result = legacy_plan(workspace, inventory)
        self._assert_blocked(workspace, result, "missing_evid_id")
        self.assert_unchanged(before, workspace)

    def test_f20b_no_evidence_without_reason_code_blocks_the_plan(self) -> None:
        workspace = self.workspace()
        inventory = self._inventory(workspace, [
            inventory_record(
                self.BETA, status="no_evidence", vorgang_ref=None, evid_id=None,
            ),
        ])
        before = workspace.snapshot()
        result = legacy_plan(workspace, inventory)
        self._assert_blocked(workspace, result, "missing_reason_code")
        self.assert_unchanged(before, workspace)

    def test_f20c_shared_evid_for_two_messages_blocks_the_plan(self) -> None:
        workspace = self.workspace()
        inventory = self._inventory(workspace, [
            inventory_record(self.ALPHA, evid_id="E-SHARED"),
            inventory_record(self.BETA, vorgang_ref="V-2", evid_id="E-SHARED"),
        ])
        before = workspace.snapshot()
        result = legacy_plan(workspace, inventory)
        self.assertEqual(result.exit_code, 2, msg=json.dumps(result.envelope))
        self.assertTrue(result.success)
        self.assertFalse(result.data.get("ok"))
        self.assertIsNone((result.data.get("approval") or {}).get("token"))
        codes = {(conflict["message_id"], conflict["code"]) for conflict in result.data["conflicts"]}
        self.assertEqual({
            (self.ALPHA, "evid_shared_conflict"), (self.BETA, "evid_shared_conflict"),
        }, codes)
        self.assert_unchanged(before, workspace)

    def test_f20d_clean_set_dry_run_apply_stale_token_and_noop_rerun(self) -> None:
        workspace = self.workspace(ledger_lines=[
            ledger_line(self.ALPHA, vorgang_ref="V-1", evid_id="E-1"),
        ])
        inventory = self._inventory(workspace, [
            inventory_record(self.ALPHA, vorgang_ref="V-1", evid_id="E-1"),
            inventory_record(self.BETA, vorgang_ref="V-2", evid_id="E-2"),
        ])
        before_plan = workspace.snapshot()
        plan = legacy_plan(workspace, inventory)
        self.assertEqual(plan.exit_code, 0, msg=json.dumps(plan.envelope))
        self.assertTrue(plan.data.get("ok"))
        self.assertFalse(plan.data.get("blocked"))
        self.assertEqual(plan.data["summary"]["import"], 1)
        self.assertEqual(plan.data["summary"]["already_present"], 1)
        token = (plan.data.get("approval") or {}).get("token")
        self.assertIsNotNone(token, msg="a clean plan must issue an approval token")
        assert_no_host_paths(plan.data)
        self.assert_unchanged(before_plan, workspace)

        # A token is stale as soon as the inventory/ledger/handoff state changes.
        self._inventory(workspace, [
            inventory_record(self.ALPHA, vorgang_ref="V-1", evid_id="E-1"),
            inventory_record(self.BETA, vorgang_ref="V-2", evid_id="E-2"),
            inventory_record(
                self.GAMMA, status="no_evidence", vorgang_ref=None, evid_id=None,
                reason_code="legacy_status_no_evidence",
            ),
        ])
        before_stale = workspace.snapshot()
        injected = InjectedLease()
        with injected:
            stale = legacy_apply(
                workspace, inventory, approval=token, lease_id=injected.lease_id,
            )
        self.assertEqual(stale.exit_code, 2, msg=json.dumps(stale.envelope))
        self.assertEqual(stale.reason_code, "legacy_import_approval_stale")
        self.assert_unchanged(before_stale, workspace)

        fresh_plan = legacy_plan(workspace, inventory)
        self.assertEqual(fresh_plan.exit_code, 0, msg=json.dumps(fresh_plan.envelope))
        self.assertEqual(fresh_plan.data["summary"]["import"], 2)
        fresh_token = (fresh_plan.data.get("approval") or {}).get("token")
        self.assertIsNotNone(fresh_token)
        self.assertNotEqual(fresh_token, token, msg="a stale token was reissued")

        before_apply = workspace.snapshot()
        with injected:
            applied = legacy_apply(
                workspace, inventory, approval=fresh_token, lease_id=injected.lease_id,
            )
        self.assertEqual(applied.exit_code, 0, msg=json.dumps(applied.envelope))
        self.assertEqual(applied.data.get("status"), "applied")
        self.assertEqual(applied.data.get("applied"), 2)
        changed = self.changed_files(before_apply, workspace)
        self.assertEqual(changed, [LEDGER_REL], msg="the import wrote a report or side file")

        lines = {line["message_id"]: line for line in self.ledger_lines(workspace)}
        self.assertEqual({self.ALPHA, self.BETA, self.GAMMA}, set(lines))
        self.assertEqual(lines[self.ALPHA]["source"], "synthesis", msg="an existing line changed")
        self.assertEqual(lines[self.BETA]["source"], "legacy_import")
        self.assertEqual(lines[self.GAMMA]["source"], "legacy_import")
        self.assertEqual(lines[self.GAMMA]["outcome"], "no_evidence")
        self.assertEqual(lines[self.GAMMA]["reason_code"], "legacy_status_no_evidence")
        self.assertNotIn("evid_id", lines[self.GAMMA])

        repeat_plan = legacy_plan(workspace, inventory)
        self.assertEqual(repeat_plan.exit_code, 0, msg=json.dumps(repeat_plan.envelope))
        self.assertEqual(repeat_plan.data["summary"]["import"], 0)
        before_repeat = workspace.snapshot()
        with injected:
            repeat = legacy_apply(
                workspace, inventory, approval=fresh_token, lease_id=injected.lease_id,
            )
        self.assertEqual(repeat.exit_code, 0, msg=json.dumps(repeat.envelope))
        self.assertEqual(repeat.data.get("status"), "already_complete")
        self.assertEqual(repeat.data.get("applied"), 0)
        self.assert_unchanged(before_repeat, workspace)
        self.assertEqual(len(self.ledger_lines(workspace)), 3)

    def test_f20d_declared_legacy_inventory_tool_agrees_with_the_plan(self) -> None:
        # The declared inventory tool's own report becomes the expectation for
        # plan/apply: record count, status distribution and conflict accounting are
        # compared counter by counter.  The report is not a plan input format and
        # the adapter declares no such seam, so the binding is asserted, not fed
        # through; the same input file is used for both calls.
        workspace = self.workspace("synth-a")
        inventory = self._inventory(workspace, [
            inventory_record(self.ALPHA, vorgang_ref="V-1", evid_id="E-1"),
            inventory_record(
                self.GAMMA, status="no_evidence", vorgang_ref=None, evid_id=None,
                reason_code="legacy_status_no_evidence",
            ),
        ])
        before = workspace.snapshot()
        report = inventory_legacy(workspace, inventory)
        self.assertEqual(report.exit_code, 0, msg=json.dumps(report.envelope))
        self.assertTrue(report.data.get("ok"))
        self.assertTrue(report.data.get("read_only"))
        self.assertNotIn(self.ALPHA, json.dumps(report.envelope))
        self.assertNotIn(self.GAMMA, json.dumps(report.envelope))
        self.assertEqual(
            [entry["item_ref"] for entry in report.data["records"]],
            [item_ref(self.ALPHA), item_ref(self.GAMMA)],
        )
        record_count = report.data["record_count"]
        status_counts = {
            status: count
            for status, count in report.data["status_counts"].items() if count
        }
        self.assertEqual(record_count, 2)
        self.assertEqual(status_counts, {"synthesized": 1, "no_evidence": 1})
        self.assertEqual(report.data["conflicts"]["problem_records"], 0)
        self.assertEqual(report.data["conflicts"]["shared_evid_ids"], 0)
        self.assert_unchanged(before, workspace)

        plan = legacy_plan(workspace, inventory)
        self.assertEqual(plan.exit_code, 0, msg=json.dumps(plan.envelope))
        self.assertEqual(plan.data["inventory"]["count"], record_count)
        entry_statuses: dict[str, int] = {}
        for entry in plan.data["entries"]:
            entry_statuses[entry["outcome"]] = entry_statuses.get(entry["outcome"], 0) + 1
        self.assertEqual(
            entry_statuses, status_counts,
            msg="the plan's status distribution disagrees with the inventory tool",
        )
        self.assertEqual(
            plan.data["summary"]["import"] + plan.data["summary"]["already_present"],
            record_count,
        )
        token = (plan.data.get("approval") or {}).get("token")
        self.assertIsNotNone(token, msg="a clean plan must issue an approval token")
        self.assert_unchanged(before, workspace)

        injected = InjectedLease()
        with injected:
            applied = legacy_apply(
                workspace, inventory, approval=token, lease_id=injected.lease_id,
            )
        self.assertEqual(applied.exit_code, 0, msg=json.dumps(applied.envelope))
        self.assertEqual(applied.data.get("applied"), plan.data["summary"]["import"])
        self.assertEqual(len(self.ledger_lines(workspace)), plan.data["summary"]["import"])

        # The same binding on a blocked set: the tool's conflict counters and the
        # plan's conflict entries describe the same inventory file.
        blocked_workspace = self.workspace("synth-b")
        blocked_inventory = self._inventory(blocked_workspace, [
            inventory_record(self.ALPHA, evid_id="E-SHARED"),
            inventory_record(self.BETA, vorgang_ref="V-2", evid_id="E-SHARED"),
            inventory_record(self.GAMMA, evid_id=None),
        ])
        before_blocked = blocked_workspace.snapshot()
        blocked_report = inventory_legacy(blocked_workspace, blocked_inventory)
        self.assertEqual(blocked_report.exit_code, 0, msg=json.dumps(blocked_report.envelope))
        self.assertEqual(blocked_report.data["record_count"], 3)
        self.assertEqual(blocked_report.data["conflicts"]["shared_evid_ids"], 1)
        self.assertEqual(
            blocked_report.data["conflicts"]["problem_counts"], {"missing_evid_id": 1},
        )
        blocked_plan = legacy_plan(blocked_workspace, blocked_inventory)
        self.assertEqual(blocked_plan.exit_code, 2, msg=json.dumps(blocked_plan.envelope))
        self.assertEqual(blocked_plan.data["inventory"]["count"], blocked_report.data["record_count"])
        self.assertIsNone((blocked_plan.data.get("approval") or {}).get("token"))
        plan_codes: dict[str, int] = {}
        for conflict in blocked_plan.data["conflicts"]:
            plan_codes[conflict["code"]] = plan_codes.get(conflict["code"], 0) + 1
        self.assertEqual(
            plan_codes, {"evid_shared_conflict": 2, "missing_evid_id": 1},
            msg="the plan's conflict accounting disagrees with the inventory tool",
        )
        self.assert_unchanged(before_blocked, blocked_workspace)


if __name__ == "__main__":  # pragma: no cover - manual single-module run
    unittest.main()
