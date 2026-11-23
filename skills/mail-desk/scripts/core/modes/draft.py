"""Draft-mode handler for the mail-desk batch runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..attachment_evaluation import attachment_evaluate, decision_triggers_evaluation
from ..attachment_reclassification import (
    discard_inventory_contradicting_evaluations,
    install_draft_attachment_evaluations,
)
from ..classifier import classify_email, draft_manifest
from ..batch_contract import add_draft_contract
from ..common import atomic_write_json, resolve_data_dir
from ..context_consultation import add_context_consultation_metadata
from ..himalaya import (
    eml_full_text_parity,
    fetch_raw_message_eml,
    get_single_email_details,
    raise_if_transport_blocked,
)
from ..injection_barrier import assess_and_quarantine, assess_email, load_injection_policy
from ..progress import BatchProgressTracker
from ..scan_source_reuse import ScanSourceContext
from ..sent_indexer import load_sent_index


def _dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
    default: Callable[..., Any],
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    return default


def _required_dependency(
    dependencies: Mapping[str, Callable[..., Any]] | None,
    name: str,
) -> Callable[..., Any]:
    if dependencies and name in dependencies:
        return dependencies[name]
    raise RuntimeError(f"{name} must be supplied by the batch-runner compatibility adapter")


def _purpose_bound(reader: Callable[..., Any], purpose: str) -> Callable[..., Any]:
    """Bind one process-start purpose to a reader without changing its contract.

    The purpose is telemetry only and never changes the reader's result.  An
    injected draft reader must accept the ``purpose`` keyword; the binding uses
    ``setdefault`` and deliberately does not swallow a ``TypeError``.
    """

    def bound(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("purpose", purpose)
        return reader(*args, **kwargs)

    return bound


def _full_text_from_source(
    context: ScanSourceContext,
    reader: Callable[..., Any],
    envelope_id: str,
    folder: str,
    account: str | None,
    fallback_envelope: Mapping[str, Any] | None,
    preview_lines: int,
    policy: Any,
) -> dict[str, Any] | None:
    """Return full details from an exactly bound scan source, or ``None``.

    The derivation uses the injected reader (default: the canonical helper) with
    only the existing pure ``raw_eml`` branch, so it starts no export and no
    read; the reader contract therefore accepts ``raw_eml`` and ``full_body``.
    The wrapper is bound only for the explicitly gated full-text opt-in
    (``allow_scan_source_full_text=True``).  The bytes must belong to the
    modelled, not live-proven full-text class (:func:`eml_full_text_parity`),
    and the first-time full text must pass the injection barrier: a parity
    rejection, quarantine, truncated scan or unprovable assessment returns
    ``None`` and the regular full read runs.  No new quarantine path and no
    review-hash change is introduced.  A structured transport block is
    re-raised, never degraded into a fallback.
    """
    if not isinstance(fallback_envelope, Mapping):
        return None
    expected = str(fallback_envelope.get("message_id") or "").strip()
    if not expected:
        return None
    eml = context.lookup(envelope_id, folder, account, expected)
    if eml is None:
        return None
    if not eml_full_text_parity(eml):
        return None
    try:
        details = reader(
            envelope_id,
            folder,
            account,
            preview_lines,
            fallback_envelope=dict(fallback_envelope),
            full_body=True,
            raw_eml=eml,
        )
    except Exception as exc:  # noqa: BLE001 - an unproven derivation keeps the regular read
        raise_if_transport_blocked(exc)
        return None
    if not isinstance(details, dict) or details.get("error") is not None:
        return None
    try:
        assessment = assess_email(details, policy)
    except Exception as exc:  # noqa: BLE001 - no proven barrier coverage means no reuse
        raise_if_transport_blocked(exc)
        return None
    if assessment.quarantined or assessment.scan_truncated:
        return None
    return details


def _source_reuse_full_reader(
    reader: Callable[..., Any],
    context: ScanSourceContext,
    policy: Any,
    default_account: str | None,
) -> Callable[..., Any]:
    """Consult the scan source before the regular full read; never bypass it."""

    def bound(*args: Any, **kwargs: Any) -> Any:
        envelope_id = str(args[0]) if args else str(kwargs.get("env_id") or "")
        folder = args[1] if len(args) > 1 else kwargs.get("folder", "INBOX")
        account = args[2] if len(args) > 2 else kwargs.get("account", default_account)
        preview_lines = int(args[3]) if len(args) > 3 else int(kwargs.get("preview_lines", 30))
        if kwargs.get("full_body"):
            reused = _full_text_from_source(
                context,
                reader,
                envelope_id,
                str(folder or "INBOX"),
                account,
                kwargs.get("fallback_envelope"),
                preview_lines,
                policy,
            )
            if reused is not None:
                return reused
        return reader(*args, **kwargs)

    return bound


def run_draft_mode(
    config: dict[str, Any],
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    dependencies: Mapping[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Fetch or reuse inspections, then create the reviewable batch manifest."""
    resolve_data = _dependency(dependencies, "resolve_data_dir", resolve_data_dir)
    progress_tracker = _dependency(dependencies, "BatchProgressTracker", BatchProgressTracker)
    load_sent = _dependency(dependencies, "load_sent_index", load_sent_index)
    draft = _dependency(dependencies, "draft_manifest", draft_manifest)
    full_reader = _dependency(dependencies, "get_single_email_details", get_single_email_details)
    write_json = _dependency(dependencies, "atomic_write_json", atomic_write_json)
    evaluate_backend = _dependency(dependencies, "attachment_evaluate", attachment_evaluate)
    reclassify = _dependency(dependencies, "classify_email", classify_email)
    raw_mime_reader = _dependency(dependencies, "fetch_raw_message_eml", fetch_raw_message_eml)
    triggers_evaluation = _dependency(
        dependencies, "decision_triggers_evaluation", decision_triggers_evaluation
    )
    add_consultation_metadata = _dependency(
        dependencies, "add_context_consultation_metadata", add_context_consultation_metadata
    )

    # Issue #103 (D1 first slice): tag the two draft-path readers with their
    # purpose.  This is telemetry only; it changes no result, target or gate.
    full_reader = _purpose_bound(full_reader, "full_read")
    raw_mime_reader = _purpose_bound(raw_mime_reader, "evaluation_raw")

    dd = data_dir or resolve_data()
    workspace_root = dd.parent.parent
    folder = config.get("folder", "INBOX")
    count = int(config.get("count", 20))
    order = str(config.get("order", "oldest")).lower()
    date = config.get("date")
    query = config.get("query")
    preview_lines = int(config.get("preview_lines", 30))
    skip_known = bool(config.get("skip_known", True))
    evaluate_attachments = config.get("evaluate_attachments", True)
    if not isinstance(evaluate_attachments, bool):
        raise ValueError("evaluate_attachments must be a boolean")
    # Issue #103 P1: draft-only opt-in, inactive by default until a real,
    # anonymized CLI reference or an explicit owner release exists.
    allow_eml_details = config.get("allow_eml_details", False)
    if not isinstance(allow_eml_details, bool):
        raise ValueError("allow_eml_details must be a boolean")
    # Issue #103 P2: the full-text reuse class is modelled, not live-proven, so
    # it stays a separate draft-only opt-in (default False) exactly like P1.
    # The evaluation reuse is independent and stays active.
    allow_scan_source_full_text = config.get("allow_scan_source_full_text", False)
    if not isinstance(allow_scan_source_full_text, bool):
        raise ValueError("allow_scan_source_full_text must be a boolean")
    # Issue #103 P3: the explicit scan profile selects the scan sequence.  Only
    # ``known_heavy`` reads a preview first; ``balanced`` is the unchanged path.
    scan_profile = config.get("scan_profile", "balanced")
    if scan_profile not in ("balanced", "known_heavy"):
        raise ValueError("scan_profile must be 'balanced' or 'known_heavy'")
    # All draft configuration is validated before any mailbox read, classification,
    # evaluation or write, so a malformed request can never perform side effects.
    expected_count = config.get("expected_count", count)
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1:
        raise ValueError("expected_count must be a positive integer")
    allow_fewer = config.get("allow_fewer", False)
    if not isinstance(allow_fewer, bool):
        raise ValueError("allow_fewer must be a boolean")
    output_file = config.get("output_file", str(dd / "batch-manifest.json"))
    inspected_file = config.get("inspected_file") or (dd / "batch-inspected.json")
    tracker = progress_tracker(mode="draft", total_items=count, data_dir=dd)
    emails: list[dict[str, Any]] = []

    if not config.get("force_fetch") and not date and not query and Path(inspected_file).exists():
        try:
            with Path(inspected_file).open("r", encoding="utf-8") as inspected_handle:
                inspected_data = json.load(inspected_handle)
            inspected_emails = inspected_data.get("emails", [])
            unprocessed = (
                [email for email in inspected_emails if not email.get("is_known")]
                if skip_known else inspected_emails
            )
            if unprocessed:
                emails = unprocessed[:count]
        except Exception:
            emails = []

    # Issue #103 P2: exactly one private source store per fresh scan.  A reused
    # inspected_file keeps no context, and the finally path drops every byte.
    source_context: ScanSourceContext | None = None
    try:
        if not emails:
            get_unprocessed = _required_dependency(dependencies, "get_unprocessed_emails")
            # Issue #103 P2: only a positive scan target can hold sources; a
            # non-positive count keeps the unchanged behaviour without a context.
            if count >= 1:
                source_context = ScanSourceContext(target_count=count)
            emails, _ = get_unprocessed(
                folder=folder,
                target_count=count,
                order=order,
                date=date,
                query=query,
                account=account,
                data_dir=dd,
                skip_known=skip_known,
                preview_lines=preview_lines,
                tracker=tracker,
                allow_eml_details=allow_eml_details,
                source_context=source_context,
                scan_profile=scan_profile,
            )

        tracker.step("classifying_and_checking_sent")
        sent_lookup = load_sent(dd)
        # Issue #62: deterministic pre-filter before any mail text reaches the
        # classifier or an agent context.  Flagged items are recorded as
        # quarantine evidence and never enter the reviewable manifest; the
        # hash-bound top-level block makes the stop visible to execute and review.
        barrier_policy = load_injection_policy(workspace_root)
        assessments, quarantine_results, barrier_block = assess_and_quarantine(
            emails,
            policy=barrier_policy,
            data_dir=dd,
            mode="draft",
        )
        clean_emails = [
            email
            for email, assessment in zip(emails, assessments)
            if not assessment.quarantined
        ]
        quarantined = barrier_block["quarantined_count"] > 0
        if source_context is not None:
            # Only the final, clean selection of this one scan may keep a source;
            # known, skipped and quarantined items are dropped here.
            source_context.retain_selected(clean_emails, account=account)
            # The evaluation reuse stays active; the full-text reuse is the
            # separately gated, modelled class and binds only on explicit opt-in.
            if allow_scan_source_full_text:
                full_reader = _source_reuse_full_reader(
                    full_reader, source_context, barrier_policy, account
                )
        # FR-15/MD-E2-T01: the classifier reports the effective source it actually used for each
        # final initial decision (preview email, or full-read email when it re-read the envelope)
        # through an explicitly transient, non-persisted channel.
        effective_sources: list[dict[str, Any]] = []
        manifest = draft(
            clean_emails,
            workspace_root=workspace_root,
            sent_lookup=sent_lookup,
            full_reader=full_reader,
            account=account,
            source_sink=effective_sources.append,
        )
        # The two-pass classification completes first; only then may a still ambiguous item enter
        # the MD-E1 evaluation and exactly one reclassification against its effective full source.
        install_draft_attachment_evaluations(
            manifest.get("items", []),
            effective_sources,
            evaluate_attachments=evaluate_attachments,
            workspace_root=workspace_root,
            data_dir=dd,
            account=account,
            evaluate=evaluate_backend,
            reclassify=reclassify,
            read_raw_mime=raw_mime_reader,
            triggers_evaluation=triggers_evaluation,
            policy=config.get("attachment_policy"),
            run_id=config.get("attachment_run_id"),
            lease_id=config.get("lease_id"),
            conversation_id=config.get("conversation_id"),
            source_lookup=(
                source_context.lookup if source_context is not None else None
            ),
        )
        # Field-consistency gate (FR-17/MD-R3): keep attachments[]/attachment_status/
        # attachment_error and attachment_evaluation/files[] mutually consistent before the
        # manifest contract is attached.  The MD-E2 trigger gate already prevents a
        # contradiction; this guard keeps the invariant true for every composed manifest.
        discard_inventory_contradicting_evaluations(manifest.get("items", []))
        # Issue #64: advisory, read-only consultation hints (existing event dossiers and
        # declared cloud storage IDs).  They never alter routing, evidence or targets.
        add_consultation_metadata(manifest.get("items", []), workspace_root=workspace_root)
        # The block is attached before the review hash so the exact quarantine state
        # is review-bound.  A later execute/sign self-check rejects it fail-closed.
        manifest["injection_barrier"] = barrier_block
        manifest = add_draft_contract(
            manifest,
            expected_count=expected_count,
            allow_fewer=allow_fewer,
            source_folder=folder,
            account=account,
            skip_known=skip_known,
        )
        output_path = Path(output_file).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(output_path, manifest)
        total_drafted = len(manifest.get("items", []))
        is_empty = total_drafted == 0 and not quarantined
        tracker.complete(f"Drafted {total_drafted} items to {output_path.name}")
        return {
            "ok": not quarantined,
            "mode": "draft",
            "status": "quarantined" if quarantined else "completed",
            "message": (
                "Draft stopped for human review: flagged mail is quarantined and "
                "excluded from the manifest; resolve it and re-draft."
                if quarantined
                else f"Drafted {total_drafted} items."
            ),
            "folder": folder,
            "order": order,
            "total_drafted": total_drafted,
            "is_empty": is_empty,
            # An empty INBOX batch is the terminal inbox-zero signal for the SOP.
            "inbox_zero": is_empty and str(folder).strip().upper() == "INBOX",
            "expected_count": expected_count,
            "allow_fewer": manifest["allow_fewer"],
            "candidate_count": manifest["candidate_count"],
            "source_folder": folder,
            "account": account,
            "skip_known": skip_known,
            "review": manifest["review"],
            "manifest_file": str(output_path),
            "draft": manifest,
            "injection_barrier": barrier_block,
            "quarantine_evidence": quarantine_results,
        }
    finally:
        # No byte and no identity entry outlives this one scan, even on an error.
        if source_context is not None:
            source_context.clear()
