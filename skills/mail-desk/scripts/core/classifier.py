"""Conservative classifier and manifest drafting for mail-desk."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .attachment_handoff import (
    HandoffDriftError,
    apply_attachment_handoff_to_item,
    build_attachment_analysis_handoff,
    validate_attachment_handoff,
)
from .attachments import AttachmentInventoryValidationError, canonicalize_and_bind_attachments
from .common import normalize_message_id, resolve_data_dir, resolve_evidence_dir, resolve_final_index_path
from .index import load_final_index
from .matching import ambiguity, project_matching, topic_matching
from .matching.date_parser import parse_date_to_year_month
# Project/artifact/evidence callables stay importable from the facade by object identity.
from .matching.project_matching import (
    _artifact_candidate,
    _artifact_code_matches,
    _artifact_text_matches,
    _build_project_evidence,
    _catalog_artifact_title,
    _evidence_read_escalation,
    _project_context_label,
    _select_project_artifacts,
    select_project_match,
)
# Topic/subtopic/operation/event/evidence callables stay importable from the facade by
# object identity and are owned by core.matching.topic_matching.
from .matching.topic_matching import (
    _build_event_evidence,
    _build_operation_evidence,
    _build_topic_evidence,
    _event_context_label,
    _event_validation_reasons,
    _has_canonical_operation_reference,
    _operation_context_label,
    _safe_event_dossier_target,
    _safe_operation_reference_target,
    _safe_subtopic_reference_target,
    _select_subtopic_event,
    _select_subtopic_operation,
    _select_topic_subtopic,
    _subject_signal_matches,
    _topic_context_label,
    _topic_parent_subject_signal,
    materialize_topic_details,
    select_topic_match,
)
from .sent_indexer import check_if_replied, load_sent_index, sync_sent_items


FULL_BODY_ARTIFACT_SIGNALS = (
    "qm plan",
    "draft",
    "handbook",
    "deliverable",
    "agreement",
    "red flags",
    "audit",
    "focus group",
    "fokusgruppe",
)

FULL_BODY_ACTION_REQUEST = re.compile(
    r"\b(?:please|kindly|could you|can you|action required|please respond|bitte|kannst du|können sie)\b",
    re.IGNORECASE,
)

#: The one documented special routing target for newsletter-suppressed mail (FR-17/MD-R1).
#: No project/topic catalog entry declares it; a ``newsletter`` ``do_not_route_if``
#: suppression with no stronger candidate maps deterministically onto this existing folder.
NEWSLETTER_TARGET_FOLDER = "Newsletter"


def load_catalogs(workspace_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load projects and topics from catalogs in workspace memory."""
    projects_file = workspace_root / "memory" / "references" / "projects" / "projects.json"
    topics_file = workspace_root / "memory" / "references" / "topics" / "topics.json"

    projects: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []

    if projects_file.exists():
        try:
            with projects_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    projects = data
                elif isinstance(data, dict):
                    projects = data.get("projects", [])
        except Exception:
            pass

    if topics_file.exists():
        try:
            with topics_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    topics = data
                elif isinstance(data, dict):
                    topics = data.get("topics", [])
        except Exception:
            pass

    return projects, topics


def full_body_triggers(email: dict[str, Any], preview_item: dict[str, Any]) -> list[str]:
    """Return documented, deterministic reasons to re-read one preview in full."""
    text = "\n".join(
        str(email.get(field, ""))
        for field in ("subject", "from", "to", "cc", "preview")
    ).casefold()
    triggers = [
        signal
        for signal in FULL_BODY_ARTIFACT_SIGNALS
        if _artifact_text_matches(text, signal)
    ]
    decision = preview_item.get("decision", {})
    for field in ("workpackage", "task", "deliverable", "milestone"):
        value = decision.get(field)
        if isinstance(value, str) and value.strip():
            triggers.append(value.casefold())
    if FULL_BODY_ACTION_REQUEST.search(text):
        triggers.append("visible_action_or_reply_request")
    if decision.get("kind") == "unknown" or decision.get("confidence") == "low":
        triggers.append("insufficient_preview_evidence")
    return list(dict.fromkeys(triggers))


def _full_read_failure(
    preview_item: dict[str, Any],
    triggers: list[str],
    error: object,
) -> dict[str, Any]:
    """Fail closed: leave a full-read failure in review, never as a certain result."""
    decision = dict(preview_item.get("decision", {}))
    decision["confidence"] = "low"
    decision["review_required"] = True
    decision["read_escalation"] = {
        "level": "full_body",
        "triggers": triggers,
        "status": "failed",
        "error": {"type": type(error).__name__, "message": str(error)},
    }
    preview_item["decision"] = decision
    preview_item["action"] = {"type": "keep_in_folder", "target_folder": "INBOX"}
    preview_item["evidence"] = None
    preview_item["notes"] = f"{preview_item.get('notes', '')} Volltextabruf fehlgeschlagen; Review erforderlich.".strip()
    return preview_item


def classify_email_two_pass(
    email: dict[str, Any],
    *,
    workspace_root: Path | None = None,
    projects: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
    sent_lookup: dict[str, Any] | None = None,
    final_index: dict[str, Any] | None = None,
    full_reader: Callable[..., dict[str, Any]] | None = None,
    account: str | None = None,
    source_sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Classify a preview, optionally re-read the same envelope, then classify again.

    ``source_sink`` (FR-15/MD-E2-T01) is an optional, explicitly transient channel: when
    supplied, it is called exactly once with the effective source email that produced the
    returned item's final decision -- the preview email when no full read happened, or the
    full-read email when it did.  The channel is never persisted and is not part of the
    manifest schema.
    """
    def _finish(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        if source_sink is not None:
            source_sink(source)
        return item

    preview_item = classify_email(
        email,
        workspace_root=workspace_root,
        projects=projects,
        topics=topics,
        sent_lookup=sent_lookup,
        final_index=final_index,
        account=account,
    )
    triggers = full_body_triggers(email, preview_item)
    if not triggers:
        return _finish(preview_item, email)

    # Keep the classifier/manifest layer deterministic. Mailbox I/O is an
    # explicit orchestration concern: draft, pipeline and inspect-propose pass
    # a reader, while library callers without one remain preview-only.
    if full_reader is None:
        return _finish(preview_item, email)

    envelope_id = str(email.get("envelope_id", "")).strip()
    if not envelope_id:
        return _finish(
            _full_read_failure(preview_item, triggers, ValueError("Missing envelope_id for full message read.")),
            email,
        )

    try:
        full_email_details = full_reader(
            envelope_id,
            str(email.get("folder", "INBOX")),
            account,
            fallback_envelope=email,
            full_body=True,
        )
        if not isinstance(full_email_details, dict):
            raise TypeError("Full message reader returned no email object.")
        if full_email_details.get("error"):
            raise RuntimeError(str(full_email_details["error"]))
        full_email = dict(email)
        for field in (
            "message_id", "raw_message_id", "subject", "from", "to", "date",
            "in_reply_to", "references", "preview", "attachments",
            "attachment_status", "attachment_error"
        ):
            if field in full_email_details:
                full_email[field] = full_email_details[field]
        full_item = classify_email(
            full_email,
            workspace_root=workspace_root,
            projects=projects,
            topics=topics,
            sent_lookup=sent_lookup,
            final_index=final_index,
            account=account,
        )
        full_item["decision"]["read_escalation"] = {
            "level": "full_body",
            "triggers": triggers,
            "status": "completed",
        }
        # Domain evidence rebuilding is owned by the matching modules; the facade keeps the
        # full-reader I/O and two-pass orchestration and only routes the reclassified decision.
        full_read_context = {"workspace_root": workspace_root or Path.cwd(), "email": full_email}
        full_evidence: dict[str, Any] | None = None
        kind = full_item["decision"].get("kind")
        if kind == "project":
            full_evidence = project_matching.resolve_full_read_project_evidence(
                projects or [], decision=full_item["decision"], **full_read_context
            )
        elif kind == "topic":
            full_evidence = topic_matching.resolve_full_read_topic_evidence(
                topics or [], decision=full_item["decision"], **full_read_context
            )
        if full_evidence is not None:
            full_item["evidence"] = full_evidence
        return _finish(full_item, full_email)
    except Exception as exc:  # noqa: BLE001 - the manifest must retain reviewable failure context
        return _finish(_full_read_failure(preview_item, triggers, exc), email)


def classify_email(
    email: dict[str, Any],
    workspace_root: Path | None = None,
    projects: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
    sent_lookup: dict[str, Any] | None = None,
    final_index: dict[str, Any] | None = None,
    account: str | None = None,
    untrusted_external_text: str | None = None,
) -> dict[str, Any]:
    """Classify a single email conservatively and determine recommended target folder and evidence.

    ``untrusted_external_text`` is an optional, already-escaped/encapsulated context (the
    validated FR-15/MD-E2 attachment handoff).  It participates in content matching only and
    is never merged into the trusted header fields or persisted as raw extraction output.
    """
    ws = workspace_root or Path.cwd()
    if projects is None or topics is None:
        p, t = load_catalogs(ws)
        projects = projects if projects is not None else p
        topics = topics if topics is not None else t

    if sent_lookup is None:
        try:
            sent_lookup = load_sent_index(ws / "data" / "mail-desk")
        except Exception:
            sent_lookup = None

    if final_index is None:
        try:
            idx_p = resolve_final_index_path(data_dir=ws / "data" / "mail-desk")
            final_index = load_final_index(idx_p)
        except Exception:
            final_index = None

    subject = str(email.get("subject", "")).strip()
    from_str = str(email.get("from", "")).strip()
    to_str = str(email.get("to", "")).strip()
    cc_str = str(email.get("cc", "")).strip()
    preview = str(email.get("preview", "")).strip()
    date_str = str(email.get("date", "")).strip()
    envelope_id = str(email.get("envelope_id", "")).strip()
    raw_mid = email.get("message_id") or email.get("raw_message_id", "")
    norm_mid = normalize_message_id(raw_mid)

    ym, ymd = parse_date_to_year_month(date_str)
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    # FR-15/MD-E2: a validated attachment handoff may be supplied as a distinct,
    # encapsulated untrusted_external source.  It is appended to the local matching
    # buffer only -- never into the trusted header fields above, catalog data or
    # authorization data -- and is not part of the returned item.
    untrusted_external = str(untrusted_external_text or "")
    if untrusted_external:
        full_text = f"{full_text}\n{untrusted_external}"
    full_text_lower = full_text.lower()
    forwarded_senders = re.findall(
        r"(?:>>>|Von:|From:)\s*[^<>\n]*<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>",
        preview,
        re.IGNORECASE,
    )
    parties = f"{from_str} {to_str} {cc_str} {' '.join(forwarded_senders)}".lower()

    # Default fallback
    target_folder = "INBOX"
    decision = {
        "kind": "unknown",
        "id": "unclassified",
        "confidence": "low",
        "needs_reply": False,
    }
    notes = ""
    evidence_spec: dict[str, Any] | None = None
    selected_project: dict[str, Any] | None = None
    selected_topic: dict[str, Any] | None = None
    synthesis_targets: list[dict[str, str]] = []
    preselected_subtopic_resolution: dict[str, Any] | None = None
    suppressed_candidates: list[dict[str, Any]] = []

    # Check for reply requirement directed at Martin
    needs_reply = False
    reply_triggers = [
        "martin bitte", "bitte martin", "frage an martin", "hallo martin", "lieber martin",
        "martin kannst du", "martin ?", "martin, bitte", "@martin"
    ]
    for trigger in reply_triggers:
        if trigger in full_text_lower:
            needs_reply = True
            break

    # Automated / no-reply senders never require a manual reply
    if "no-reply" in from_str.lower() or "do_not_reply" in from_str.lower() or "quarantine" in from_str.lower() or "mailer-daemon" in from_str.lower():
        needs_reply = False

    # --------------------------------------------------------------------------
    # 0. Thread-Inheritance Fast-Path (In-Reply-To & References against Master Index)
    # --------------------------------------------------------------------------
    thread_matched = False
    in_reply_to_raw = email.get("in_reply_to", "")
    references_raw = email.get("references", "")

    raw_ref_strs = []
    if isinstance(in_reply_to_raw, str) and in_reply_to_raw:
        raw_ref_strs.append(in_reply_to_raw)
    elif isinstance(in_reply_to_raw, list):
        raw_ref_strs.extend([str(x) for x in in_reply_to_raw if str(x)])

    if isinstance(references_raw, str) and references_raw:
        raw_ref_strs.append(references_raw)
    elif isinstance(references_raw, list):
        raw_ref_strs.extend([str(x) for x in references_raw if str(x)])

    ref_mids: list[str] = []
    combined_refs = " ".join(raw_ref_strs)
    if combined_refs:
        for match in re.finditer(r"<([^>]+)>|([^\s<>]+@[^\s<>]+)", combined_refs):
            cand = match.group(1) or match.group(2)
            if cand:
                nc = normalize_message_id(cand)
                if nc and nc not in ref_mids:
                    ref_mids.append(nc)

    indexed_items = (final_index or {}).get("items", {})
    parent_item = None
    parent_mid = None
    for r_mid in ref_mids:
        if r_mid in indexed_items:
            cand_parent = indexed_items[r_mid]
            cand_folder = cand_parent.get("final_folder")
            if cand_folder and cand_folder != "INBOX":
                parent_item = cand_parent
                parent_mid = r_mid
                break

    if parent_item:
        parent_folder = parent_item.get("final_folder", "")
        # Parent-folder mapping stays ordered project-before-topic; owners build the state.
        thread_context = {
            "workspace_root": ws,
            "email": email,
            "needs_reply": needs_reply,
            "parent_mid": str(parent_mid or ""),
        }
        inheritance = project_matching.match_thread_project_inheritance(
            projects or [], parent_folder, **thread_context
        )
        if inheritance is None:
            inheritance = topic_matching.match_thread_topic_inheritance(
                topics or [], parent_folder, **thread_context
            )
        if inheritance is not None:
            selected_project = inheritance.get("project")
            selected_topic = inheritance.get("topic")
            target_folder = inheritance["target_folder"]
            decision = inheritance["decision"]
            notes = inheritance["notes"]
            evidence_spec = inheritance["evidence"]
            thread_matched = True
        else:
            target_folder = parent_folder
            decision = {
                "kind": "other",
                "id": parent_folder,
                "confidence": "high",
                "needs_reply": needs_reply,
            }
            notes = f"Thread-Vererbung via In-Reply-To ({str(parent_mid or '')[:20]}...) zu {parent_folder}."
            thread_matched = True

    # --------------------------------------------------------------------------
    # 1. Dynamic Project Catalog Matching (High / Medium confidence)
    # --------------------------------------------------------------------------
    matched_project = None

    if not thread_matched:
        # Root project-catalog matching is owned by core.matching.project_matching.
        root_project_match = select_project_match(
            projects or [],
            subject=subject,
            full_text=full_text,
            full_text_lower=full_text_lower,
            from_str=from_str,
            to_str=to_str,
            parties=parties,
            cc_str=cc_str,
            suppressed_candidates=suppressed_candidates,
        )
        if root_project_match:
            matched_project = root_project_match
            pid = root_project_match["id"]
            target_folder = root_project_match["folder"]
            decision = {
                "kind": "project",
                "id": pid,
                "confidence": root_project_match["confidence"],
                "needs_reply": needs_reply,
            }
            notes = f"Projektbezogene Abstimmung zu {root_project_match['name'].upper()} (Betreff: {subject})."

            ev_dir = resolve_evidence_dir("projects", pid, workspace_root=ws)
            try:
                ev_dir_rel = str(ev_dir.relative_to(ws).as_posix())
            except ValueError:
                ev_dir_rel = str(ev_dir.as_posix())
            ev_file_rel = f"{ev_dir_rel}/{ym}.md"
            ev_entry = (
                f"- {ymd} — {subject}.\n"
                f"  - Message-ID: `{norm_mid}` ({from_str})\n"
                f"  - Aussagekern: {notes}\n"
                f"  - Einordnung: Dokumentation der laufenden Projektkommunikation zu {pid.upper()}."
            )
            evidence_spec = {
                "file": ev_file_rel,
                "entry": ev_entry,
            }
            selected_project = root_project_match["catalog"]

    # --------------------------------------------------------------------------
    # 2. Dynamic Topic Catalog Matching (High / Medium confidence)
    # --------------------------------------------------------------------------
    if not thread_matched and not matched_project:
        # Root topic-catalog matching and the unique-subtopic fallback/override are owned
        # by core.matching.topic_matching.  The exact winning catalog object crosses the
        # seam so the detail step below needs no second inline topic-catalog lookup.
        matched_topic = select_topic_match(
            topics or [],
            subject=subject,
            full_text=full_text,
            full_text_lower=full_text_lower,
            from_str=from_str,
            to_str=to_str,
            parties=parties,
            cc_str=cc_str,
            suppressed_candidates=suppressed_candidates,
        )
        if matched_topic:
            target_folder = matched_topic["folder"]
            decision = {
                "kind": "topic",
                "id": matched_topic["id"],
                "confidence": matched_topic["confidence"],
                "needs_reply": needs_reply,
            }
            notes = f"Themenbezogene Zuordnung zu {matched_topic['title']} (Betreff: {subject})."
            selected_topic = matched_topic["catalog"]
            preselected_subtopic_resolution = matched_topic["preselected_subtopic"]

    # --------------------------------------------------------------------------
    # 2-sys. Automated System Notifications & Junk Filtering
    # --------------------------------------------------------------------------
    if not thread_matched and not matched_project and not matched_topic:
        if (
            re.search(r"ist dem Meeting beigetreten|has joined the meeting", subject, re.IGNORECASE)
            and ("zoom.us" in from_str.lower() or "zoom" in full_text_lower)
        ):
            target_folder = "Trash"
            decision = {
                "kind": "notification",
                "id": "zoom-join-ping",
                "confidence": "high",
                "needs_reply": False,
            }
            notes = f"Automatisierte Zoom-Beitrittsbenachrichtigung (ephemer): {subject}"
        elif (
            re.search(r"Meeting-Objekte für .* sind bereit|Cloud-Aufzeichnung.*verfügbar|recording.*is now available", subject, re.IGNORECASE)
            and ("zoom.us" in from_str.lower() or "zoom" in full_text_lower)
        ):
            target_folder = "Themen/BOKU-Organisation"
            decision = {
                "kind": "topic",
                "id": "boku-organisation",
                "confidence": "medium",
                "needs_reply": False,
            }
            notes = f"Zoom-Aufzeichnungsbenachrichtigung zu BOKU-Organisation: {subject}"
        elif (
            re.search(r"\burgent inquiry\b|\bkindly clarify\b|\bconfidential proposal\b|\bfinancial assistance\b|\bbeneficiary\b", subject, re.IGNORECASE)
            and any(freemail in from_str.lower() for freemail in ["@yahoo.", "@hotmail.", "@live.", "@aol.", "@mail.ru"])
            and not any(boku_kw in full_text_lower for boku_kw in ["weiterbildung", "lebenslanges lernen", "focus group", "lehrgang"])
        ):
            target_folder = "Junk"
            decision = {
                "kind": "spam",
                "id": "junk-freemailer",
                "confidence": "high",
                "needs_reply": False,
            }
            notes = f"Spam/Phishing-Klassifikation: {subject}"

    # --------------------------------------------------------------------------
    # 2-dnr. Catalog do_not_route_if outcome (FR-17 / MD-R1)
    # --------------------------------------------------------------------------
    if decision.get("kind") == "unknown" and suppressed_candidates:
        if any(row.get("suppression_reason") == "newsletter" for row in suppressed_candidates):
            target_folder = NEWSLETTER_TARGET_FOLDER
            decision = {
                "kind": "unknown",
                "id": "unclassified",
                "confidence": "low",
                "needs_reply": needs_reply,
                "review_required": False,
            }
        else:
            target_folder = "INBOX"
            decision = {
                "kind": "unknown",
                "id": "unclassified",
                "confidence": "low",
                "needs_reply": needs_reply,
                "review_required": True,
                "review_reason": "do_not_route_suppressed",
            }
        notes = ""

    # --------------------------------------------------------------------------
    # 2a. Project artifact matching (FR-02a)
    # --------------------------------------------------------------------------
    # Thread inheritance establishes only the project root.  Current-mail artifact
    # context is always resolved from the current visible mail fields.
    if selected_project is not None and decision.get("kind") == "project":
        artifact_resolution = _select_project_artifacts(selected_project, full_text)
        for field in ("workpackage", "task", "deliverable", "milestone"):
            if field in artifact_resolution:
                decision[field] = artifact_resolution[field]
        if "match_reasons" in artifact_resolution:
            decision["artifact_match_reasons"] = artifact_resolution["match_reasons"]
        if "candidates" in artifact_resolution:
            decision["artifact_candidates"] = artifact_resolution["candidates"]
        evidence_spec = _build_project_evidence(
            selected_project,
            decision,
            workspace_root=ws,
            year_month=ym,
            date=ymd,
            subject=subject,
            message_id=norm_mid,
            from_str=from_str,
            to_str=to_str,
        )

    # --------------------------------------------------------------------------
    # 2b. Topic subtopic matching (FR-03a)
    # --------------------------------------------------------------------------
    # Parent routing is intentionally complete before this step. Subtopic signals
    # therefore refine context only and can never select another mailbox folder.
    if selected_topic is not None and decision.get("kind") == "topic":
        # Subtopic/operation/event enrichment, domain evidence, event validation,
        # cross-kind conflict handling and safe synthesis targets are owned by
        # core.matching.topic_matching.materialize_topic_details.
        topic_details = materialize_topic_details(
            selected_topic,
            decision,
            subject=subject,
            full_text=full_text,
            preselected_subtopic=preselected_subtopic_resolution,
            workspace_root=ws,
            year_month=ym,
            date=ymd,
            message_id=norm_mid,
            from_str=from_str,
            to_str=to_str,
        )
        decision = topic_details["decision"]
        # A subtopic-scoped detail step yields its own evidence; when no current-mail
        # subtopic exists, any prior evidence (e.g. thread inheritance) is retained.
        if topic_details["evidence"] is not None:
            evidence_spec = topic_details["evidence"]
        synthesis_targets = topic_details["synthesis_targets"]

    # --------------------------------------------------------------------------
    # 3. Sent Items Reply Check
    # --------------------------------------------------------------------------
    if sent_lookup and (needs_reply or decision.get("needs_reply")):
        reply_info = check_if_replied(email, sent_lookup)
        if reply_info:
            if reply_info.get("replied"):
                needs_reply = False
                decision["needs_reply"] = False
                decision["replied_via_sent"] = reply_info
                notes = f"{notes} (Bereits beantwortet via Sent Items: {reply_info.get('sent_subject', '')})"
            elif reply_info.get("has_candidate"):
                cand = reply_info.get("candidate", {})
                decision["reply_candidate"] = cand
                cand_subj = cand.get("sent_subject", "")
                cand_date = str(cand.get("sent_date", ""))[:10]
                cand_to = cand.get("matched_recipient", "")
                notes = f"{notes} [Antwort-Kandidat: '{cand_subj}' am {cand_date} an {cand_to}]"

    raw_attachments = email.get("attachments", [])
    bound_attachments: list[dict[str, Any]] = []
    inventory_error: str | None = None

    item_account = account or email.get("account")
    if raw_attachments:
        if not item_account or not str(item_account).strip():
            raise ValueError(
                f"Missing account for envelope {envelope_id}: cannot create manifest with attachment "
                f"candidates without a bound and verified account."
            )
        try:
            bound_attachments = canonicalize_and_bind_attachments(
                raw_attachments=raw_attachments,
                account=str(item_account).strip(),
                folder=email.get("folder", "INBOX"),
                envelope_id=envelope_id,
                message_id=norm_mid or raw_mid,
            )
        except AttachmentInventoryValidationError as exc:
            inventory_error = f"Invalid attachment inventory: {exc}"
            bound_attachments = []
        except ValueError as exc:
            if "account" in str(exc).lower():
                raise
            inventory_error = f"Invalid attachment binding: {exc}"
            bound_attachments = []

    attachment_failed = (
        email.get("attachment_status") == "attachment_inventory_unavailable"
        or bool(email.get("attachment_error"))
        or bool(inventory_error)
    )

    if attachment_failed:
        target_folder = "INBOX"
        decision = {
            "kind": "unknown",
            "id": "unclassified",
            "confidence": "low",
            "needs_reply": needs_reply,
            "review_required": True,
            "review_reason": "attachment_inventory_unavailable",
        }
        att_err = inventory_error or email.get("attachment_error") or "MIME attachment inventory unavailable"
        notes = f"[Review: Anhangs-Inventarisierung nicht verfügbar ({att_err})] {notes}".strip()
        evidence_spec = None
        synthesis_targets = []
        bound_attachments = []

    if suppressed_candidates:
        decision["suppressed_candidates"] = suppressed_candidates

    action = {
        "type": "copy_as_move" if target_folder != "INBOX" else "keep_in_folder",
        "target_folder": target_folder,
    }

    if attachment_failed:
        att_status = "attachment_inventory_unavailable"
    else:
        att_status = email.get("attachment_status") or ("available" if bound_attachments else "none")

    item_result: dict[str, Any] = {
        "envelope_id": envelope_id,
        "source_folder": email.get("folder", "INBOX"),
        "message_id": norm_mid,
        "raw_message_id": raw_mid,
        "subject": subject,
        "from": from_str,
        "to": email.get("to", ""),
        "date": date_str,
        "in_reply_to": email.get("in_reply_to", ""),
        "references": email.get("references", []),
        "action": action,
        "decision": decision,
        "notes": notes or f"Klassifikation: {subject}",
        "evidence": evidence_spec,
        "synthesis_targets": synthesis_targets,
        "attachments": bound_attachments,
        "attachment_status": att_status,
    }
    final_att_err = inventory_error or email.get("attachment_error")
    if final_att_err:
        item_result["attachment_error"] = final_att_err

    handoff = email.get("attachment_analysis_handoff")
    if handoff:
        items = handoff.get("items") if isinstance(handoff, Mapping) else None
        if items and not bound_attachments:
            raise HandoffDriftError(
                "attachment_analysis_handoff with items cannot be verified: missing verified bound_attachments from mail inventory"
            )
        norm_identity = {
            "account": item_account,
            "message_id": norm_mid or raw_mid,
            "folder": email.get("folder", "INBOX"),
            "envelope_id": envelope_id,
        }
        handoff = validate_attachment_handoff(
            handoff,
            mail_identity=norm_identity,
            decision=decision,
            canonical_parts=bound_attachments if bound_attachments else None,
        )
    elif email.get("attachment_extractions"):
        if not bound_attachments:
            # Untrusted extractions without mail inventory -> fail-closed into Review in INBOX
            item_result["action"] = {"type": "keep_in_folder", "target_folder": "INBOX"}
            decision["review_required"] = True
            decision["review_reason"] = "untrusted_attachment_extractions_without_inventory"
            decision["confidence"] = "low"
            notes_prefix = "[Review: Anhänge ohne Inventarbindung nicht vertrauenswürdig] "
            if not item_result["notes"].startswith(notes_prefix):
                item_result["notes"] = notes_prefix + item_result["notes"]
            handoff = None
        else:
            handoff = build_attachment_analysis_handoff(
                mail_identity={
                    "account": item_account,
                    "message_id": norm_mid or raw_mid,
                    "folder": email.get("folder", "INBOX"),
                    "envelope_id": envelope_id,
                },
                attachments=email["attachment_extractions"],
                decision=decision,
                canonical_parts=bound_attachments,
                default_materiality=email.get("materiality") or email.get("attachment_materiality"),
            )

    if handoff:
        apply_attachment_handoff_to_item(item_result, handoff)

    return item_result


def draft_manifest(
    emails: list[dict[str, Any]],
    workspace_root: Path | None = None,
    delete_input_on_success: bool = True,
    sent_lookup: dict[str, Any] | None = None,
    sync_sent: bool = True,
    final_index: dict[str, Any] | None = None,
    full_reader: Callable[..., dict[str, Any]] | None = None,
    account: str | None = None,
    source_sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Generate a batch manifest dictionary from a list of inspected emails.

    ``source_sink`` (FR-15/MD-E2-T01) is an optional transient pass-through: when supplied it
    receives the effective source email for each item's final initial classification.  It is
    never part of the returned manifest.
    """
    ws = workspace_root or Path.cwd()
    projects, topics = load_catalogs(ws)
    dd = ws / "data" / "mail-desk"

    if final_index is None:
        try:
            idx_p = resolve_final_index_path(data_dir=dd)
            final_index = load_final_index(idx_p)
        except Exception:
            final_index = None

    if sent_lookup is None:
        if sync_sent and emails:
            try:
                batch_dates = [parse_date_to_year_month(str(e.get("date", "")))[1] for e in emails if e.get("date")]
                if batch_dates:
                    sync_sent_items(dates=batch_dates, data_dir=dd, workspace_root=ws)
            except Exception:
                pass
        try:
            sent_lookup = load_sent_index(dd)
        except Exception:
            sent_lookup = None

    items: list[dict[str, Any]] = []
    for email in emails:
        item = classify_email_two_pass(
            email,
            workspace_root=ws,
            projects=projects,
            topics=topics,
            sent_lookup=sent_lookup,
            final_index=final_index,
            full_reader=full_reader,
            account=account,
            source_sink=source_sink,
        )
        items.append(item)

    return {
        "mode": "execute",
        "delete_input_on_success": delete_input_on_success,
        "items": items,
    }
