"""Sent items indexer and thread response matcher."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import atomic_rewrite_jsonl, atomic_write_json, normalize_message_id, utc_now_iso, resolve_data_dir
from .himalaya import HimalayaInvocationError, run_himalaya, get_single_email_details
from .sent_handoff import (
    HANDOFF_FILENAME,
    MAX_HANDOFF_ITEMS,
    SentHandoffError,
    SentHandoffPublishError,
    SentIndexAppendError,
    SentStateError,
    SentSyncPartialFailure,
    build_handoff_item,
    durable_append_jsonl,
    load_ledger,
    norm_id,
    read_handoff,
    resolve_thread_anchor,
    tolerant_jsonl_records,
    unconsumed,
    write_handoff,
)
from .matching.reply_heuristics import (
    DEFAULT_NO_REPLY_SENDER_TOKENS,
    DEFAULT_SENT_SENDER_DOMAIN_WHITELIST,
    DEFAULT_SENT_SUBJECT_STOPWORDS,
    ReplyHeuristicsConfig,
    load_reply_heuristics,
)

#: Maximum number of days synchronized in one watermark-derived Sent-Items run.
MAX_SYNC_WINDOW_DAYS = 60

#: Himalaya page size used for the per-date Sent-Items envelope query.
SENT_SYNC_PAGE_SIZE = 100

#: Maximum pages fetched per date.  A still-full last page is fail-closed (no
#: watermark advance); the error names a higher-limit Python-API recovery call.
SENT_SYNC_MAX_PAGES = 50

#: Strict adapter end signal for requesting a page beyond the last full page.
#: Only the classified Himalaya command error with exactly this documented text is
#: accepted; any other error (including transport "buffer index out of bounds") is
#: fail-closed.  Pattern is anchored to the adapter's formatted stderr message.
_SENT_PAGE_END_PATTERN = re.compile(
    r"^Himalaya failed: Error: (?:envelope )?page out of bounds?\.?$", re.IGNORECASE
)


def _is_verified_page_end(error: BaseException) -> bool:
    """Whether ``error`` is the documented, classified end-of-pages signal."""
    return (
        isinstance(error, HimalayaInvocationError)
        and error.reason_code == "himalaya_command_failed"
        and bool(_SENT_PAGE_END_PATTERN.match(str(error)))
    )


# Re-exported canonical MD-ID1 identity defaults.  These are the same objects the
# reply-heuristics owner defines -- imported, never duplicated -- so the sent-index
# identity stays sourced from the workspace desk-signals catalog owner.


def _default_identity_config() -> ReplyHeuristicsConfig:
    """The documented MD-ID1 default identity, built without any catalog file I/O."""
    return ReplyHeuristicsConfig(
        reply_triggers=(),
        no_reply_sender_tokens=DEFAULT_NO_REPLY_SENDER_TOKENS,
        sent_sender_domain_whitelist=DEFAULT_SENT_SENDER_DOMAIN_WHITELIST,
        sent_subject_stopwords=DEFAULT_SENT_SUBJECT_STOPWORDS,
    )


def clean_subject(subj: str) -> str:
    """Strip reply/forward prefixes and excess whitespace."""
    s = subj.strip()
    prefix_re = re.compile(r"^\s*(re|aw|antw|wg|fwd|wtrlt)\s*:\s*", re.IGNORECASE)
    while prefix_re.match(s):
        s = prefix_re.sub("", s).strip()
    return re.sub(r"\s+", " ", s).lower()


def load_sent_index(data_dir: Path | None = None) -> dict[str, Any]:
    """Load sent-index.jsonl into structured lookup maps."""
    dd = data_dir or resolve_data_dir()
    sent_path = dd / "sent-index.jsonl"

    by_message_id: dict[str, dict[str, Any]] = {}
    by_in_reply_to: dict[str, list[dict[str, Any]]] = {}
    by_reference: dict[str, list[dict[str, Any]]] = {}
    by_subject_clean: dict[str, list[dict[str, Any]]] = {}
    all_entries: list[dict[str, Any]] = []

    if not sent_path.exists():
        return {
            "by_message_id": by_message_id,
            "by_in_reply_to": by_in_reply_to,
            "by_reference": by_reference,
            "by_subject_clean": by_subject_clean,
            "all_entries": all_entries,
        }

    # Per-line decoding: a crash-torn multi-byte character in the last line is
    # skipped as junk instead of making the whole index unreadable.
    for entry in tolerant_jsonl_records(sent_path):
        mid = normalize_message_id(str(entry.get("message_id", "")))
        if not mid:
            continue

        entry["norm_message_id"] = mid
        all_entries.append(entry)
        by_message_id[mid] = entry

        irt = normalize_message_id(str(entry.get("in_reply_to", "")))
        if irt:
            by_in_reply_to.setdefault(irt, []).append(entry)

        for ref in entry.get("references", []):
            ref_norm = normalize_message_id(str(ref))
            if ref_norm:
                by_reference.setdefault(ref_norm, []).append(entry)

        subj = entry.get("subject", "")
        if subj:
            cs = clean_subject(subj)
            if cs:
                by_subject_clean.setdefault(cs, []).append(entry)

    return {
        "by_message_id": by_message_id,
        "by_in_reply_to": by_in_reply_to,
        "by_reference": by_reference,
        "by_subject_clean": by_subject_clean,
        "all_entries": all_entries,
    }


def append_sent_index_entries(
    entries: list[dict[str, Any]],
    data_dir: Path | None = None,
) -> int:
    """Append new entries to sent-index.jsonl, skipping duplicates by message_id."""
    if not entries:
        return 0

    dd = data_dir or resolve_data_dir()
    sent_path = dd / "sent-index.jsonl"
    sent_path.parent.mkdir(parents=True, exist_ok=True)

    existing_mids: set[str] = set()
    for e in tolerant_jsonl_records(sent_path):
        mid = normalize_message_id(str(e.get("message_id", "")))
        if mid:
            existing_mids.add(mid)

    new_entries: list[dict[str, Any]] = []
    for entry in entries:
        mid = normalize_message_id(str(entry.get("message_id", "")))
        if not mid or mid in existing_mids:
            continue
        existing_mids.add(mid)
        new_entries.append(entry)

    return durable_append_jsonl(sent_path, new_entries)


def _known_index_ids(dd: Path) -> tuple[set[str], set[str]]:
    """Message-IDs and Sent envelope ids already present in the sent-index."""
    index = load_sent_index(dd)
    eids = {str(e.get("sent_envelope_id", "")) for e in index["all_entries"] if e.get("sent_envelope_id")}
    return set(index["by_message_id"].keys()), eids


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    """Oldest source window first; then header date; envelope id as tie-break.

    The source window (the date query that returned the envelope) is authoritative
    for coverage/barrier decisions; a missing, contradictory or unparseable Date
    header must not move an item across days.
    """
    entry = candidate["entry"]
    window = str(candidate.get("window_date") or "")
    parsed = parse_date_to_datetime(str(entry.get("at", "")))
    eid = str(entry.get("sent_envelope_id", ""))
    return (
        not bool(window), window,
        parsed is None, parsed or datetime.min,
        int(eid) if eid.isdigit() else -1, eid,
    )


def _commit_sent_candidates(dd: Path, candidates: list[dict[str, Any]], *, account: str | None) -> dict[str, Any]:
    """Hand off, then index, the newly detected Sent Items (in that order).

    Order is the crash contract (process crash/kill): the handoff is persisted
    atomically BEFORE the sent-index grows.  A crash between the two leaves the items
    in the handoff and not in the index, so a rerun re-detects exactly them and merges
    idempotently by Message-ID.  A crash after the index but before consumption leaves
    them pending in the handoff until the ledger releases them.

    Write rule (no write before every refusal check passed): the handoff file is
    replaced only when this run has new items to hand off AND no unconsumed item is
    pending, so pending items are never overwritten, extended or pruned.  Consumed
    items still listed in the file are ignored (state is derived from the ledger) and
    disappear with the next replacement.  No new items means no handoff write.

    Fail-closed before any write (``SentHandoffError``): invalid handoff or ledger,
    pending items of another mailbox and pending items plus further new items
    (``handoff_pending``).  More new items than one handoff may hold are committed in
    deterministic batches (oldest first); the suffix stays unindexed and re-readable.
    """
    ledger = load_ledger(dd)
    consumed = ledger["by_message_id"]

    existing = read_handoff(dd)
    pending = unconsumed(existing, ledger)
    consumed_ignored = len(existing) - len(pending)
    pending_ids = {item["norm_message_id"] for item in pending}
    state_before = "pending" if pending else "none"
    if account and any(item["mailbox"] != account for item in pending):
        raise SentHandoffError(
            "handoff_invalid",
            f"Sent handoff '{HANDOFF_FILENAME}' holds unconsumed item(s) of another mailbox than the "
            "bound account (mailbox_mismatch); it was left untouched and nothing was indexed.",
            {"reason": "mailbox_mismatch", "pending_count": len(pending), "state": "invalid"},
        )

    ordered = sorted(candidates, key=_candidate_sort_key)
    extra = [
        c for c in ordered
        if norm_id(c["entry"].get("message_id")) not in consumed
        and norm_id(c["entry"].get("message_id")) not in pending_ids
    ]
    if pending and extra:
        raise SentHandoffError(
            "handoff_pending",
            f"Sent handoff '{HANDOFF_FILENAME}' still holds {len(pending)} unconsumed item(s); "
            f"{len(extra)} newly detected Sent Item(s) were NOT handed off and NOT indexed. "
            "Recovery: read `core.sent_handoff.load_pending_handoff(data_dir)`, promote and call "
            "`record_synthesized(data_dir, items)` for the pending items, then rerun sync-sent.",
            {
                "pending_count": len(pending),
                "blocked_new_count": len(extra),
                "consumed_ignored": consumed_ignored,
                "state": "pending",
            },
        )

    # Handoff is bounded (200).  More new items: commit only the prefix up to the
    # first deferred item, so no newer consumed/candidate item can advance the
    # watermark past an older deferral.  The rest stays unindexed for the next run.
    if len(extra) > MAX_HANDOFF_ITEMS:
        first_deferred_id = norm_id(extra[MAX_HANDOFF_ITEMS]["entry"].get("message_id"))
        split_index = next(
            index for index, candidate in enumerate(ordered)
            if norm_id(candidate["entry"].get("message_id")) == first_deferred_id
        )
    else:
        split_index = len(ordered)
    committed = ordered[:split_index]
    deferred = ordered[split_index:]
    deferred_days = sorted({
        str(candidate["window_date"])
        for candidate in deferred
        if candidate.get("window_date")
    })
    if deferred and not deferred_days:
        raise SentHandoffError(
            "sent_window_unidentified",
            "A deferred Sent item has no source-window date; completeness of the batch cannot be "
            "proven. Nothing was handed off or indexed. Recovery: rerun through the normal collector "
            "so each candidate carries its queried date.",
            {"deferred_count": len(deferred), "state": state_before},
        )
    new_items: list[dict[str, Any]] = []
    new_handoff_items: list[dict[str, Any]] = []
    for candidate in committed:
        item = build_handoff_item(
            candidate["entry"],
            to=candidate["to"],
            cc=candidate["cc"],
            headers_complete=candidate["headers_complete"],
        )
        item["anchored_to"] = resolve_thread_anchor(item, ledger)
        already = item["norm_message_id"] in consumed
        new_items.append({**item, "already_synthesized": already})
        if not already and item["norm_message_id"] not in pending_ids:
            new_handoff_items.append(item)

    all_items = [*pending, *new_handoff_items]
    if new_handoff_items:
        try:
            write_handoff(dd, all_items)  # only reached with no pending item; BEFORE the index grows
        except SentHandoffError:
            raise  # explicit pre-write refusal (bound) keeps its reason code
        except Exception as error:  # noqa: BLE001 - the atomic replace may already have published
            raise SentHandoffPublishError(
                f"Sent handoff write failed and may already have replaced '{HANDOFF_FILENAME}' "
                f"({type(error).__name__}: {error}); the on-disk handoff state is unverified and a "
                "rerun validates it and merges idempotently by Message-ID."
            ) from error
    try:
        append_sent_index_entries([c["entry"] for c in committed], dd)
    except Exception as error:  # noqa: BLE001 - any append failure leaves unverified progress
        raise SentIndexAppendError(
            f"Sent-index append failed after the handoff was persisted ({type(error).__name__}: {error}); "
            "the handoff is retained, the index progress is unknown and a rerun merges idempotently by Message-ID."
        ) from error

    return {
        "new_items": new_items,
        "handoff": {
            "state": "written" if new_handoff_items else ("pending" if all_items else "none"),
            "written_count": len(new_handoff_items),
            "pending_count": len(all_items),
            "consumed_ignored": consumed_ignored,
            "already_synthesized_skipped": sum(1 for item in new_items if item["already_synthesized"]),
            # Items deferred to the next run after consuming this 200-item batch.
            "deferred_count": len(deferred),
            "headers_incomplete_count": sum(1 for item in new_items if not item["headers_complete"]),
            "ledger_torn_tail": ledger["torn_tail"],
            "max_items": MAX_HANDOFF_ITEMS,
        },
        "deferred_days": deferred_days,
    }


def _sync_sent_items_for_date(
    date_str: str,
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    max_pages: int = SENT_SYNC_MAX_PAGES,
) -> dict[str, Any]:
    """Fetch one Sent-Items date, hand off and index its new items.

    Returns ``{"added", "envelopes_examined", "truncated", "new_items", "handoff"}``
    (``handoff`` is ``None`` when the day had nothing new).  A Himalaya or
    envelope-parse failure is fail-closed: it re-raises with the date in the
    message instead of masquerading as ``0 new mails``.  A legitimately empty
    day (``[]``) is not an error and returns ``added: 0``.

    Fails loud with a ``ValueError`` before any file write if ``account`` is
    missing: every indexed entry must be bound to the verified workspace account
    rather than a hardcoded mailbox.
    """
    dd = data_dir or resolve_data_dir()
    known_mids, known_eids = _known_index_ids(dd) if account and str(account).strip() else (set(), set())
    candidates, stats = _collect_date_candidates(
        date_str, folder=folder, account=account, known_mids=known_mids, known_eids=known_eids,
        max_pages=max_pages,
    )
    stats = {**stats, "new_items": [], "handoff": None, "added": 0}
    if candidates:
        committed = _commit_sent_candidates(dd, candidates, account=account)
        stats.update(added=len(committed["new_items"]), new_items=committed["new_items"], handoff=committed["handoff"])
    return stats


def _collect_date_candidates(
    date_str: str,
    *,
    folder: str,
    account: str | None,
    known_mids: set[str],
    known_eids: set[str],
    max_pages: int = SENT_SYNC_MAX_PAGES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read one Sent-Items date (no writes) and return the not-yet-indexed candidates.

    Envelopes are paginated (adapter convention ``--page``/``--page-size``) until a
    short or empty page proves completeness; envelope ids are de-duplicated across
    pages.  ``known_mids``/``known_eids`` are updated in place so overlapping dates
    of one run never yield the same message twice.  A full page limit without proof
    of completeness is fail-closed (``sent_window_truncated``) before any write and
    names a concrete Python-API recovery call.
    """
    if account is None or not str(account).strip():
        raise ValueError(
            "Missing account: cannot bind sent-index entries to a mailbox without "
            "a verified account. Bind an account before syncing Sent Items."
        )

    # Date query via Himalaya (fast single-day filter).  Pages are fetched until a
    # short/empty page: this is the adapter convention `envelope list --page N`
    # with `--page-size`.  Any page error is fail-closed, never a silent partial.
    pages = 0
    envs_by_id: dict[str, dict[str, Any]] = {}
    try:
        while pages < max_pages:
            pages += 1
            out = run_himalaya(
                ["-o", "json", "envelope", "list", "-f", folder,
                 "-s", str(SENT_SYNC_PAGE_SIZE), "--page", str(pages), f"date {date_str}"],
                account=account,
                timeout=30,
            )
            if "[" in out:
                out = out[out.find("["):]
            page_envs = json.loads(out)
            if not isinstance(page_envs, list):
                raise RuntimeError("Himalaya returned a non-list envelope payload.")
            new_envs = 0
            for env in page_envs:
                eid = str(env.get("id"))
                if eid not in envs_by_id:
                    envs_by_id[eid] = env
                    new_envs += 1
            if len(page_envs) < SENT_SYNC_PAGE_SIZE:
                break
            if new_envs == 0:  # full page without progress: fail closed, never silent
                raise SentHandoffError(
                    "sent_window_truncated",
                    f"Sent sync for {date_str}: pagination made no progress on page {pages} "
                    f"({len(page_envs)} repeated envelope ids); completeness is unproven. Nothing "
                    "was handed off or indexed and the watermark did not advance. Recovery: inspect "
                    f"the adapter paging for this day and rerun "
                    f"sync_sent_items_by_dates(dates=['{date_str}'], include_next_day=False).",
                    {"date": date_str, "page_size": SENT_SYNC_PAGE_SIZE, "page": pages,
                     "envelopes_examined": len(envs_by_id)},
                )
        else:
            # Every allowed page was full and no short/empty page proved the end.
            raise SentHandoffError(
                "sent_window_truncated",
                f"Sent sync for {date_str} still returned full envelope pages after {max_pages} "
                f"page(s) ({SENT_SYNC_PAGE_SIZE} each); completeness is unproven. Nothing was handed "
                "off or indexed and the watermark did not advance. Recovery: rerun this day with a "
                f"higher page limit via the Python API "
                f"sync_sent_items_by_dates(dates=['{date_str}'], include_next_day=False, "
                f"max_pages={max_pages * 2}).",
                {
                    "date": date_str,
                    "page_size": SENT_SYNC_PAGE_SIZE,
                    "pages": max_pages,
                    "envelopes_examined": len(envs_by_id),
                    "recovery": (
                        f"sync_sent_items_by_dates(dates=['{date_str}'], include_next_day=False, "
                        f"max_pages={max_pages * 2})"
                    ),
                },
            )
    except SentHandoffError:
        raise
    except Exception as error:
        # A verified adapter end marker: asking for a page beyond the last full page
        # can fail with "out of bound".  Only this exact marker on page >= 2 counts as
        # the end; every other error remains fail-closed.
        if pages >= 2 and _is_verified_page_end(error):
            pass
        else:
            raise RuntimeError(
                f"Sent sync failed for {date_str} (page {pages}): {type(error).__name__}: {error}. "
                "Nothing was handed off or indexed. Recovery: "
                f"sync_sent_items_by_dates(dates=['{date_str}'], include_next_day=False, max_pages={max_pages})."
            ) from error

    envs = list(envs_by_id.values())
    envelopes_examined = len(envs)
    stats = {"envelopes_examined": envelopes_examined, "pages": pages, "truncated": False}
    unhandled_envs = [e for e in envs if str(e.get("id")) not in known_eids]
    candidates: list[dict[str, Any]] = []
    now_iso = utc_now_iso()

    for env in unhandled_envs:
        eid = str(env.get("id"))
        email_res = get_single_email_details(eid, folder, account=account, preview_lines=5, fallback_envelope=env)
        mid = normalize_message_id(str(email_res.get("message_id", "")))
        real_message_id = bool(mid)
        if not mid:
            mid = f"sent-env-{eid}"

        if mid in known_mids:
            continue

        raw_mid = email_res.get("raw_message_id") or mid
        raw_irt = email_res.get("in_reply_to", "")
        raw_refs = email_res.get("references", [])
        if isinstance(raw_refs, str):
            raw_refs = [r for r in raw_refs.split() if r]

        to_field = email_res.get("to", "")
        to_list = [t.strip() for t in to_field.split(",") if t.strip()] if isinstance(to_field, str) else to_field

        entry: dict[str, Any] = {
            "schema_version": 1,
            "at": email_res.get("date") or now_iso,
            "updated_at": now_iso,
            "mailbox": account,
            "message_id": f"<{raw_mid}>" if not raw_mid.startswith("<") else raw_mid,
            "in_reply_to": f"<{raw_irt}>" if raw_irt and not raw_irt.startswith("<") else (raw_irt or ""),
            "references": [f"<{r}>" if not str(r).startswith("<") else str(r) for r in raw_refs],
            "subject": email_res.get("subject", ""),
            "from": email_res.get("from", ""),
            "to": to_list,
            "folder": folder,
            "sent_envelope_id": eid,
            "backend_locator": f"{eid}|{mid}",
            "note": f"Gesendet: {email_res.get('subject', '')}",
        }
        entry_mid = norm_id(entry["message_id"])
        if entry_mid in known_mids:
            continue

        candidates.append({
            "entry": entry,
            "window_date": date_str,
            "to": to_field,
            "cc": email_res.get("cc", ""),
            # Absent key = legacy/injected reader that does not report completeness.
            "headers_complete": bool(email_res.get("headers_complete", True))
            and not email_res.get("error")
            and real_message_id,
        })
        known_mids.update({mid, entry_mid})
        known_eids.add(eid)

    return candidates, stats


def sync_sent_items_by_date(
    date_str: str,
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
    *,
    max_pages: int = SENT_SYNC_MAX_PAGES,
    since: str | None = None,
) -> int:
    """Fetch one specific date (YYYY-MM-DD); deferrals fail closed.

    Routes through the plural report path so coverage state, pagination and 200-item
    batching are never bypassed.  A deferred batch raises ``handoff_deferred`` with
    committed/deferred counts and a concrete report recovery; callers that need the
    full result use ``sync_sent_items_by_dates_report``.  A missing state with
    existing index entries requires a verified ``since``.
    """
    report = sync_sent_items_by_dates_report(
        [date_str], folder=folder, account=account, data_dir=data_dir,
        include_next_day=False, max_pages=max_pages, since=since,
    )
    if report["truncated"]:
        raise SentHandoffError(
            "handoff_deferred",
            f"Date sync committed {report['new_count']} item(s) and deferred "
            f"{report['deferred_count']}; use sync_sent_items_by_dates_report for visibility. "
            f"{report['recovery']}",
            {
                "committed_count": report["new_count"],
                "deferred_count": report["deferred_count"],
                "next_start": report["next_start"],
                "recovery": report["recovery"],
                "state": report["handoff"]["state"] if report["handoff"] else "none",
            },
        )
    return report["new_count"]


def _dedupe_iso_dates(dates: list[str]) -> list[str]:
    """Normalize a date list to sorted unique valid calendar ISO dates."""
    unique_dates: set[str] = set()
    for d in dates:
        d_clean = str(d).strip()
        if len(d_clean) != 10:
            raise SentHandoffError(
                "sync_invalid_date",
                f"Invalid date {d!r}; expected YYYY-MM-DD. Nothing was read or written. "
                "Recovery: correct the manifest/date value and rerun.",
                {"value": str(d)},
            )
        try:
            datetime.strptime(d_clean, "%Y-%m-%d")
        except ValueError as error:
            raise SentHandoffError(
                "sync_invalid_date",
                f"Invalid calendar date {d!r}; expected YYYY-MM-DD. Nothing was read or written. "
                "Recovery: correct the manifest/date value and rerun.",
                {"value": str(d)},
            ) from error
        unique_dates.add(d_clean)
    return sorted(unique_dates)


def _require_account(account: str | None) -> None:
    if account is None or not str(account).strip():
        raise ValueError(
            "Missing account: cannot bind sent-index entries to a mailbox without "
            "a verified account. Bind an account before syncing Sent Items."
        )


def _validate_since(since: str | None, run_date: datetime) -> tuple[str, datetime] | None:
    """Validate an operator-supplied verified conservative start (bare ISO date).

    A valid ``since`` is the explicit, verified lower bound used when no coverage
    state exists.  It is never mixed with a watermark or point date.
    """
    if since is None:
        return None
    since_text = str(since)
    if len(since_text) != 10 or re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_text) is None:
        raise SentHandoffError(
            "sync_invalid_since",
            f"'since' must be YYYY-MM-DD without a time part ({since_text!r}); nothing was read "
            "or written. Recovery: use a bare date on/before the run date and rerun.",
            {"invalid_field": "since", "reason": "format"},
        )
    try:
        since_dt = datetime.strptime(since_text, "%Y-%m-%d")
    except ValueError as error:
        raise SentHandoffError(
            "sync_invalid_since",
            f"'since' must be a valid YYYY-MM-DD calendar date ({since_text!r}); nothing was read "
            "or written. Recovery: correct since to a bare valid date and rerun.",
            {"invalid_field": "since", "reason": "format"},
        ) from error
    if since_dt > run_date:
        raise SentHandoffError(
            "sync_invalid_since",
            f"'since' {since!r} lies in the future of the frozen run date "
            f"{run_date:%Y-%m-%d}; nothing was read or written. Recovery: use a date on/before "
            "the run date and rerun.",
            {"invalid_field": "since", "reason": "future"},
        )
    return since_text, since_dt


def _since_hint(
    verified_since: tuple[str, datetime] | None, effective_coverage_start: str | None,
) -> dict[str, Any]:
    """Explain whether a requested ``since`` actually shaped the coverage start.

    A younger ``since`` is deliberately ignored (coverage is never skipped); the
    caller gets an explicit, additive hint instead of a silent no-op.
    """
    requested = verified_since[0] if verified_since is not None else None
    ignored = bool(
        requested is not None
        and effective_coverage_start is not None
        and requested > str(effective_coverage_start)
    )
    return {
        "since_requested": requested,
        "since_ignored": ignored,
        "since_ignored_reason": "coverage_starts_before_requested_since" if ignored else None,
        "effective_coverage_start": effective_coverage_start,
    }


def _expand_sync_dates(dates: list[str], include_next_day: bool = True) -> list[str]:
    """Expand ISO dates to the queried set, optionally including the following day."""
    expanded: set[str] = set()
    for d_str in _dedupe_iso_dates(dates):
        expanded.add(d_str)
        if include_next_day:
            try:
                next_dt = datetime.strptime(d_str, "%Y-%m-%d") + timedelta(days=1)
                expanded.add(next_dt.strftime("%Y-%m-%d"))
            except Exception:
                continue
    return sorted(expanded)


def _write_state_after_commit(dd: Path, initial: str | None, barrier: str | None) -> None:
    try:
        _write_sync_state(dd, initial_start=initial, covered_through=barrier)
    except Exception as error:  # noqa: BLE001 - index may already be committed
        raise SentIndexAppendError(
            "Coverage state write failed after the index append; the index is committed but the "
            f"coverage barrier is unverified ({type(error).__name__}: {error}). A rerun re-reads from "
            "the previous barrier and merges idempotently by Message-ID."
        ) from error


def sync_sent_items_by_dates_report(
    dates: list[str],
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
    include_next_day: bool = True,
    max_pages: int = SENT_SYNC_MAX_PAGES,
    since: str | None = None,
) -> dict[str, Any]:
    """Date-range sync with explicit coverage/batching, never a silent advance.

    A pre-read state validation/migration happens first; a conservative rewind of
    the coverage barrier is persisted **before** any mailbox read.  Point-date calls
    can only advance over a contiguous fully requested prefix and never mark today
    as complete; gaps leave the barrier untouched.

    A missing coverage state with existing index entries fails closed unless an
    explicit verified ``since`` is supplied; that value is used exactly as the
    start and is never derived from point dates, the watermark or a CLI default.
    """
    dd = data_dir or resolve_data_dir()
    run_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _require_account(account)
    verified_since = _validate_since(since, run_date)
    expanded = _expand_sync_dates(dates, include_next_day=include_next_day)
    state = _load_or_migrate_state(dd, run_date=run_date)
    initial, barrier = state["initial_start"], state["covered_through"]
    watermark = _sent_index_watermark(dd)
    has_index = _sent_index_has_entries(dd)
    state_missing = initial is None and barrier is None
    requested = sorted(set(expanded))
    if not requested:
        return {
            "new_count": 0, "new_items": [], "handoff": None, "truncated": False,
            "deferred_count": 0, "next_start": None, "recovery": None,
            "requested_dates": [], **_since_hint(verified_since, None),
        }
    earliest = requested[0]
    default_start = (run_date - timedelta(days=MAX_SYNC_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    if state_missing and has_index:
        if verified_since is None:
            raise SentHandoffError(
                "sent_initial_state_required",
                "A sent-index with existing entries has no coverage state; nothing was read or "
                "written. Recovery: call sync_sent_items_by_dates_report(dates=[...], "
                "since='YYYY-MM-DD', include_next_day=False) with an authorized verified "
                "conservative start, or restore a valid state. Point dates, the newest watermark "
                "and the CLI default are never used as a start; quarantining the state alone is "
                "insufficient.",
                {"reason": "missing_state_with_index", "index_entries_present": True},
            )
        initial, since_dt = verified_since
        barrier = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        if earliest < initial:
            raise SentHandoffError(
                "sent_initial_state_required",
                f"Requested date {earliest} lies before the verified since {initial}; nothing was "
                "read or written and no start is inferred from point dates. Recovery: use a "
                "verified conservative since on/before the requested date, or restore a valid "
                "coverage state with that start.",
                {"reason": "requested_before_verified_since",
                 "since": initial, "requested": earliest},
            )
    elif state_missing:
        # Empty index only: the earliest requested day is the explicit start; the
        # watermark is never consulted for an existing index.
        initial = min(earliest, default_start)
    coverage_start = _coverage_start(initial, barrier, watermark, run_date)
    if verified_since is not None and not (state_missing and has_index):
        since_day, since_dt = verified_since
        if since_day < coverage_start:
            initial = min([d for d in (initial, since_day) if d])
            barrier = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            coverage_start = since_day
    if earliest < coverage_start:
        initial = min([d for d in (initial, earliest) if d])
        barrier = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        coverage_start = earliest
    # Durable pre-write BEFORE the first read: a crash during/after commit leaves a
    # conservative (rewound) barrier, never a younger one that skips old items.
    _prewrite_state_if_changed(dd, initial, barrier, state, None)

    known_mids, known_eids = _known_index_ids(dd) if account and str(account).strip() else (set(), set())
    candidates: list[dict[str, Any]] = []
    for d_str in requested:
        attempt_mids, attempt_eids = set(known_mids), set(known_eids)
        date_candidates, _ = _collect_date_candidates(
            d_str, folder=folder, account=account, known_mids=attempt_mids, known_eids=attempt_eids,
            max_pages=max_pages,
        )
        known_mids, known_eids = attempt_mids, attempt_eids
        candidates.extend(date_candidates)

    if not candidates:
        new_barrier = _advance_days(requested, coverage_start, [], run_date, barrier)
        _write_sync_state(dd, initial_start=initial, covered_through=new_barrier)
        yesterday = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
        complete = bool(new_barrier is not None and new_barrier >= yesterday)
        next_start = (datetime.strptime(new_barrier, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") \
            if new_barrier is not None and new_barrier < yesterday else None
        if not complete and next_start is None:
            next_start = coverage_start
        recovery = None
        if not complete and next_start:
            recovery = (
                f"Coverage gap: continue at {next_start} with "
                f"sync_sent_items_by_dates_report(dates=['{next_start}'], include_next_day=False)."
            )
        return {
            "new_count": 0, "new_items": [], "handoff": None, "truncated": False,
            "deferred_count": 0,
            "next_start": next_start,
            "recovery": recovery, "requested_dates": list(requested),
            "coverage_start": coverage_start, "coverage_end": new_barrier,
            "covered_days": max(0, (datetime.strptime(new_barrier, "%Y-%m-%d")
                                    - datetime.strptime(coverage_start, "%Y-%m-%d")).days + 1)
            if new_barrier is not None else 0,
            "coverage_complete": complete,
            **_since_hint(verified_since, coverage_start),
        }
    committed = _commit_sent_candidates(dd, candidates, account=account)
    deferred_days = committed.get("deferred_days") or []
    truncated = bool(deferred_days)
    next_start = deferred_days[0] if deferred_days else None
    new_barrier = _advance_days(requested, coverage_start, deferred_days, run_date, barrier)
    _write_state_after_commit(dd, initial, new_barrier)
    yesterday = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
    if next_start is None and new_barrier is not None and new_barrier < yesterday:
        next_start = (datetime.strptime(new_barrier, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    recovery = None
    if truncated:
        recovery = (
            f"Consume the pending handoff, then run "
            f"sync_sent_items_by_dates_report(dates=['{next_start}'], include_next_day=False, "
            f"max_pages={max_pages}); all remaining ids are re-detected from the barrier."
        )
    elif not (new_barrier is not None and new_barrier >= yesterday):
        next_start = next_start or coverage_start
        recovery = (
            f"Coverage gap: continue at {next_start} with "
            f"sync_sent_items_by_dates_report(dates=['{next_start}'], include_next_day=False)."
        )
    return {
        "new_count": len(committed["new_items"]),
        "new_items": committed["new_items"],
        "handoff": committed["handoff"],
        "truncated": truncated,
        "deferred_count": committed["handoff"]["deferred_count"] if deferred_days else 0,
        "next_start": next_start,
        "recovery": recovery,
        "requested_dates": list(requested),
        "coverage_start": coverage_start,
        "coverage_end": new_barrier,
        "covered_days": max(0, (datetime.strptime(new_barrier, "%Y-%m-%d")
                                - datetime.strptime(coverage_start, "%Y-%m-%d")).days + 1)
        if new_barrier is not None else 0,
        "coverage_complete": bool(new_barrier is not None and new_barrier >= yesterday),
        **_since_hint(verified_since, coverage_start),
    }


def sync_sent_items_by_dates(
    dates: list[str],
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
    include_next_day: bool = True,
    max_pages: int = SENT_SYNC_MAX_PAGES,
    since: str | None = None,
) -> int:
    """Legacy int API: complete date syncs return their count; deferrals fail closed.

    Callers that need the batching/coverage result must use
    ``sync_sent_items_by_dates_report``; this wrapper never hides a deferred batch.
    A missing state with existing index entries requires a verified ``since``.
    """
    report = sync_sent_items_by_dates_report(
        dates, folder=folder, account=account, data_dir=data_dir,
        include_next_day=include_next_day, max_pages=max_pages, since=since,
    )
    if report["truncated"]:
        raise SentHandoffError(
            "handoff_deferred",
            f"Date sync committed {report['new_count']} item(s) and deferred "
            f"{report['deferred_count']}; use sync_sent_items_by_dates_report for visibility. "
            f"{report['recovery']}",
            {
                "committed_count": report["new_count"],
                "deferred_count": report["deferred_count"],
                "next_start": report["next_start"],
                "recovery": report["recovery"],
                "state": report["handoff"]["state"] if report["handoff"] else "none",
            },
        )
    return report["new_count"]


def _sent_index_watermark(data_dir: Path) -> datetime | None:
    """Return the newest indexed Sent-Items date, or ``None`` for an empty index."""
    index = load_sent_index(data_dir)
    latest: datetime | None = None
    for entry in index.get("all_entries", []):
        parsed = parse_date_to_datetime(str(entry.get("at", "")))
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _sent_index_has_entries(data_dir: Path) -> bool:
    """True when ``sent-index.jsonl`` exists with at least one non-blank line.

    Deliberately independent of a parseable ``at`` value: an existing index with
    corrupt/missing dates must never be mistaken for a fresh start.  An unreadable
    existing file also counts as existing so the caller fails closed.
    """
    path = data_dir / "sent-index.jsonl"
    try:
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    return True
    except OSError:
        return True
    return False


SYNC_STATE_FILENAME = "sent-sync-state.json"
LEGACY_INITIAL_FILENAME = "sent-sync-initial-start.json"
INITIAL_START_FILENAME = SYNC_STATE_FILENAME  # legacy alias for callers/tests


def _iso_day(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _read_sync_state(data_dir: Path) -> dict[str, str | None]:
    """Read the persisted coverage state (initial start + covered-through barrier).

    ``covered_through`` is the last day whose full day was committed.  The barrier
    only ever moves after the index append, so a crash leaves it behind (safe
    re-read) and it can never advance over an incomplete day.
    """
    path = data_dir / SYNC_STATE_FILENAME
    if not path.exists():
        return {"initial_start": None, "covered_through": None}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {
            "schema_version", "initial_start", "covered_through",
        }:
            raise ValueError("exact schema keys required")
        if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
            raise ValueError("unsupported schema_version")
        initial = _iso_day(document.get("initial_start"), "initial_start")
        covered = _iso_day(document.get("covered_through"), "covered_through")
        if initial is None and covered is None:
            raise ValueError("initial_start or covered_through is required")
        return {"initial_start": initial, "covered_through": covered}
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError) as error:
        raise SentHandoffError(
            "sent_initial_state_invalid",
            f"'{SYNC_STATE_FILENAME}' is not a valid coverage state ({type(error).__name__}: {error}); "
            "nothing was read or written and no index/handoff was changed. A valid state is "
            '{"schema_version":1,"initial_start":"YYYY-MM-DD","covered_through":null|"YYYY-MM-DD"} '
            "with a required initial_start. Recovery: an authorized operator restores a VERIFIED "
            "older initial_start (conservative re-read); quarantining the corrupt state alone is "
            "insufficient: after quarantine a verified conservative initial_start/since must be "
            "recreated before the next sync. No autonomous deletion/overwrite and no invented coverage.",
            {"reason": "unreadable_or_malformed"},
        ) from error


def _read_legacy_initial(data_dir: Path) -> str | None:
    """Read the pre-barrier legacy initial marker without modifying or deleting it."""
    path = data_dir / LEGACY_INITIAL_FILENAME
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or type(document.get("schema_version")) is not int \
                or document.get("schema_version") != 1:
            raise ValueError("unsupported schema_version")
        initial = _iso_day(document.get("initial_start"), "initial_start")
        if initial is None:
            raise ValueError("initial_start is required")
        return initial
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError) as error:
        raise SentHandoffError(
            "sent_initial_state_invalid",
            f"Legacy '{LEGACY_INITIAL_FILENAME}' is not a valid initial marker "
            f"({type(error).__name__}: {error}); nothing was read or written. A valid legacy marker "
            'is {"schema_version":1,"initial_start":"YYYY-MM-DD"}. Recovery: an authorized operator '
            "restores a verified older date; quarantining the legacy file alone is insufficient because "
            "a verified state/since must still exist (never overwritten/deleted automatically), with no "
            "invented coverage.",
            {"reason": "legacy_initial_invalid"},
        ) from error


def _load_or_migrate_state(
    data_dir: Path, tracker: dict[str, bool] | None = None, *, run_date: datetime,
) -> dict[str, str | None]:
    """Read/validate the state and migrate legacy/null-start states non-destructively.

    Missing ``initial_start`` is upgraded to ``covered_through`` (contiguous proven
    coverage); the legacy marker is merged conservatively and never rewritten or
    deleted.  Future initial/barrier values fail closed before any read/write.
    """
    state = _read_sync_state(data_dir)
    legacy = _read_legacy_initial(data_dir)
    initial, barrier = state["initial_start"], state["covered_through"]
    run_day = run_date.strftime("%Y-%m-%d")
    # Validate every ORIGINAL source before merge/overwrite; a legacy marker must not
    # mask a future new state and vice versa.
    for label, value, reason in (
        ("new initial_start", initial, "initial_start_in_future"),
        ("new covered_through", barrier, "barrier_in_future"),
        ("legacy initial_start", legacy, "legacy_initial_in_future"),
    ):
        if value is not None and datetime.strptime(value, "%Y-%m-%d") > run_date:
            raise SentHandoffError(
                "sent_initial_state_invalid",
                f"{label} '{value}' lies in the future of the frozen run date {run_day}; nothing "
                "was read or written. The valid schema is "
                '{"schema_version":1,"initial_start":"YYYY-MM-DD","covered_through":null|"YYYY-MM-DD"} '
                "with initial_start required. Recovery: an authorized operator restores a VERIFIED "
                "older initial_start; quarantining the state alone is insufficient (a verified state/"
                "since must be recreated before resync). No autonomous deletion/overwrite and no "
                "invented coverage.",
                {"reason": reason},
            )
    changed = False
    if initial is None and barrier is not None:
        # A historical point-query barrier is NOT proof of a safe lower bound: newer
        # index entries can sit above an uncovered gap.  Without an independently
        # proven boundary no migration is safe.
        raise SentHandoffError(
            "sent_initial_state_invalid",
            "Coverage state has initial_start:null with covered_through; the old barrier is "
            "not proof of contiguous coverage (historical point queries can leave gaps), so no "
            "safe migration is possible and nothing was read or written. A valid state is "
            '{"schema_version":1,"initial_start":"YYYY-MM-DD","covered_through":null|"YYYY-MM-DD"} '
            "with a verified conservative initial_start. Recovery: an authorized operator restores "
            "such a state from a verified older boundary; quarantining the state alone is insufficient "
            "(a verified state/since must be recreated before resync). No autonomous deletion/overwrite "
            "and no invented coverage.",
            {"reason": "unsafe_null_initial_migration"},
        )
    if barrier is not None and barrier >= run_day:
        # The frozen run day is never final; re-read it instead of skipping it.
        barrier = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
        changed = True
    if legacy is not None:
        merged_initial = min([d for d in (initial, legacy) if d])
        if merged_initial != initial:
            initial = merged_initial
            if barrier is not None and barrier >= initial:
                barrier = (datetime.strptime(initial, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            changed = True
    if initial is not None and datetime.strptime(initial, "%Y-%m-%d") > run_date:
        raise SentHandoffError(
            "sent_initial_state_invalid",
            f"Migrated initial_start '{initial}' lies in the future of the frozen run date {run_day}; "
            "nothing was read or written. Recovery: an authorized operator restores a verified older "
            "state; quarantining alone is insufficient and no watermark is used as a start. No "
            "autonomous deletion or invented coverage.",
            {"reason": "migrated_initial_in_future"},
        )
    if barrier is not None and datetime.strptime(barrier, "%Y-%m-%d") > run_date:
        raise SentHandoffError(
            "sent_initial_state_invalid",
            f"Migrated covered_through '{barrier}' lies in the future of the frozen run date {run_day}; "
            "nothing was read or written. Recovery: an authorized operator restores a verified older "
            "state; quarantining alone is insufficient. No autonomous deletion.",
            {"reason": "migrated_barrier_in_future"},
        )
    if changed:
        _write_sync_state(data_dir, initial_start=initial, covered_through=barrier)
        if tracker is not None:
            tracker["state_written"] = True
    return {"initial_start": initial, "covered_through": barrier}


def _write_sync_state(
    data_dir: Path, *, initial_start: str | None, covered_through: str | None,
) -> None:
    """Atomically persist the coverage state; publication may still be uncertain."""
    try:
        atomic_write_json(
            data_dir / SYNC_STATE_FILENAME,
            {
                "schema_version": 1,
                "initial_start": initial_start,
                "covered_through": covered_through,
            },
        )
    except Exception as error:  # noqa: BLE001 - atomic replace may already have published
        raise SentStateError(
            f"Coverage state write failed and may already have replaced '{SYNC_STATE_FILENAME}' "
            f"({type(error).__name__}: {error}); rerun re-reads/repairs the coverage state."
        ) from error


def _coverage_start(initial_start: str | None, barrier: str | None, watermark: datetime | None,
                    run_date: datetime) -> str:
    if barrier is not None:
        return (datetime.strptime(barrier, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if initial_start is not None:
        return initial_start
    if watermark is not None:
        return datetime(watermark.year, watermark.month, watermark.day).strftime("%Y-%m-%d")
    return (run_date - timedelta(days=MAX_SYNC_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")


def _prewrite_state_if_changed(
    dd: Path, initial: str | None, barrier: str | None, previous: dict[str, str | None],
    tracker: dict[str, bool] | None,
) -> None:
    """Durably persist a rewind/new initial start BEFORE any mailbox read."""
    if initial == previous.get("initial_start") and barrier == previous.get("covered_through"):
        return
    try:
        _write_sync_state(dd, initial_start=initial, covered_through=barrier)
    except SentStateError:
        raise
    except Exception as error:  # noqa: BLE001 - atomic replace may already have published
        raise SentStateError(
            f"Coverage state pre-write failed and may already have been published "
            f"({type(error).__name__}: {error}); rerun re-reads/repairs the coverage state."
        ) from error
    if tracker is not None:
        tracker["state_written"] = True


def _advance_days(
    requested: list[str], coverage_start: str, deferred_days: list[str],
    run_date: datetime, current_barrier: str | None,
) -> str | None:
    """Advance only over a contiguous, fully requested prefix; today is not final."""
    cursor = datetime.strptime(coverage_start, "%Y-%m-%d")
    end: str | None = None
    for day in sorted(set(requested)):
        day_dt = datetime.strptime(day, "%Y-%m-%d")
        if day_dt < cursor:
            continue
        if day_dt != cursor:
            break
        end = day
        cursor += timedelta(days=1)
    if end is not None and deferred_days:
        cutoff = (datetime.strptime(deferred_days[0], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        if end > cutoff:
            end = cutoff
    yesterday = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
    if end is not None and end > yesterday:
        end = yesterday
    if end is not None and (current_barrier is None or end > current_barrier):
        return end
    return current_barrier


def _derive_sent_sync_window(
    count: int | None,
    data_dir: Path,
    *,
    run_date: datetime,
    since: str | None = None,
) -> tuple[list[str], bool, str | None, dict[str, Any]]:
    """Build the no-dates day window from the frozen run date and resolved start.

    The caller resolves the start from the persistent coverage state (barrier,
    initial marker, watermark day or default backfill); it is passed as ``since``.
    The watermark fallback remains for direct/legacy calls.  ``count`` is an
    **item** budget applied by the caller at day boundaries; this helper always
    returns the full day window.
    """
    if since:
        try:
            start = datetime.strptime(str(since)[:10], "%Y-%m-%d")
        except (TypeError, ValueError) as error:
            raise ValueError("since must be an ISO date (YYYY-MM-DD)") from error
    else:
        if _sent_index_has_entries(data_dir):
            raise SentHandoffError(
                "sent_initial_state_required",
                "No verified start was supplied while sent-index entries exist; the window is "
                "never derived from the watermark for an existing index. Recovery: pass a verified "
                "conservative since or restore a valid coverage state.",
                {"reason": "missing_start_with_index", "index_entries_present": True},
            )
        watermark = _sent_index_watermark(data_dir)
        if watermark is None:
            start = run_date - timedelta(days=MAX_SYNC_WINDOW_DAYS - 1)
        else:
            # Unreachable with a non-empty index, kept for the empty-file case: the
            # watermark day is re-read inclusively if entries somehow appear later.
            start = datetime(watermark.year, watermark.month, watermark.day)

    if start > run_date:
        return [], False, None, {
            "coverage_start": None, "coverage_end": None, "covered_days": 0,
            "total_days": 0, "coverage_complete": True, "next_start": None,
        }

    total_days = (run_date - start).days + 1
    window = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(total_days)]
    return window, False, None, {
        "coverage_start": start.strftime("%Y-%m-%d"),
        "coverage_end": run_date.strftime("%Y-%m-%d"),
        "covered_days": total_days,
        "total_days": total_days,
        "coverage_complete": True,
        "next_start": None,
    }


def _sync_sent_items_impl(
    count: int | None = 100,
    folder: str = "Sent Items",
    dates: list[str] | None = None,
    account: str | None = None,
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
    since: str | None = None,
    max_pages: int = SENT_SYNC_MAX_PAGES,
    state_tracker: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Sync Sent Items by explicit dates or by a coverage window.

    The no-dates path freezes the run date once, then covers days oldest-first from
    the persisted coverage state, or an explicit verified ``since`` when no state
    exists.  An existing sent-index without a coverage state fails closed
    (``sent_initial_state_required``) unless that verified ``since`` is supplied;
    point dates, the newest watermark and the CLI default are never used as a
    start, and ``since`` is used exactly (never ``min(watermark, since)``).  Only a
    genuinely empty index falls back to a deterministic initial backfill of
    ``MAX_SYNC_WINDOW_DAYS`` days ending at the frozen run date.  ``count`` is an
    **item budget** applied at day boundaries: the run never reads past a day that
    would exceed it and reports ``truncated`` with a concrete per-day recovery call.
    Empty days never consume budget, so they cannot stall coverage.

    Every date is paginated (``--page``/``--page-size``) until a short or empty
    page proves completeness.  A still-full page limit is fail-closed
    (``sent_window_truncated``) with a higher-limit recovery call and no watermark
    advance.  All windows are read before the single commit; any window failure is
    fail-closed **before any write** (``SentSyncPartialFailure``, nothing handed off
    or indexed).  A successful run hands off before the sent-index grows.

    Returns ``{"date_windows_synced", "envelopes_examined",
    "new_entries_indexed", "new_count", "new_items", "handoff", "truncated",
    "coverage_start", "coverage_end", "covered_days", "total_days",
    "coverage_complete", "next_start", "truncated_days", "follow_up_hint"}``.
    ``truncated_days`` is reserved and always empty (pagination failures are
    fail-closed, day-limit truncation is reported by ``truncated``).
    """
    dd = data_dir or resolve_data_dir()
    run_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    _require_account(account)
    requested_dates: list[str] | None = None
    if dates:
        requested_dates = _expand_sync_dates(_dedupe_iso_dates(dates), include_next_day=True)
    verified_since = _validate_since(since, run_date)
    follow_up_hint: str | None = None
    truncated = False
    state = _load_or_migrate_state(dd, state_tracker, run_date=run_date)  # validates before every read/write
    initial_start = state["initial_start"]
    barrier = state["covered_through"]
    watermark = _sent_index_watermark(dd)
    has_index = _sent_index_has_entries(dd)
    state_missing = initial_start is None and barrier is None
    default_start = (run_date - timedelta(days=MAX_SYNC_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    if state_missing and has_index and verified_since is None:
        # Any existing index entry (even with an unparseable ``at``) proves prior
        # coverage; never initialise from point dates, the newest watermark or a
        # CLI default.  Only an explicitly verified since= may supply the start.
        raise SentHandoffError(
            "sent_initial_state_required",
            "A sent-index with existing entries has no coverage state; nothing was read or written. "
            "The index must never be initialised from point dates, the newest watermark or a CLI "
            "default. Recovery: rerun with an authorized verified conservative since= via "
            "sync_sent_items(since='YYYY-MM-DD', ...) or "
            "sync_sent_items_by_dates_report(dates=[...], since='YYYY-MM-DD', include_next_day=False), "
            "or restore "
            '{"schema_version":1,"initial_start":"YYYY-MM-DD","covered_through":null} with that '
            "verified start. Quarantining the state alone is insufficient; no autonomous deletion.",
            {"reason": "missing_state_with_index", "index_entries_present": True},
        )
    if dates:
        requested = requested_dates or []
        earliest = min(requested) if requested else None
        if state_missing and has_index:
            # verified_since is guaranteed by the guard above; use exactly the
            # operator value, never min(watermark, since) or a point date.
            initial_start, since_dt = verified_since
            barrier = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            if earliest is not None and earliest < initial_start:
                raise SentHandoffError(
                    "sent_initial_state_required",
                    f"Requested date {earliest} lies before the verified since {initial_start}; "
                    "nothing was read or written and no start is inferred from point dates. "
                    "Recovery: use a verified conservative since on/before the requested date, or "
                    "restore a valid coverage state with that start.",
                    {"reason": "requested_before_verified_since",
                     "since": initial_start, "requested": earliest},
                )
        elif earliest is not None and state_missing:
            # Empty index only: the earliest requested day is the explicit start;
            # the watermark is never consulted for an existing index.
            initial_start = min(earliest, default_start)
        coverage_start = _coverage_start(initial_start, barrier, watermark, run_date)
        if verified_since is not None and not (state_missing and has_index):
            # Same conservative rewind as the report API: an older verified since
            # must shape the start before any read/commit, never be silently unused.
            since_day, since_dt = verified_since
            if since_day < coverage_start:
                initial_start = min([d for d in (initial_start, since_day) if d])
                barrier = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                coverage_start = since_day
        if earliest is not None and earliest < coverage_start:
            initial_start = min([d for d in (initial_start, earliest) if d])
            barrier = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            coverage_start = earliest
        _prewrite_state_if_changed(dd, initial_start, barrier, state, state_tracker)
        query_dates = requested
        coverage: dict[str, Any] = {
            "coverage_start": coverage_start, "coverage_end": None, "covered_days": 0,
            "total_days": len(requested), "coverage_complete": False, "next_start": coverage_start,
        }
    else:
        if state_missing and has_index:
            # verified_since is guaranteed by the guard above; use exactly the
            # operator value, never min(watermark, since).
            initial_start, since_dt = verified_since
            barrier = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            base_start = initial_start
        else:
            base_start = _coverage_start(initial_start, barrier, watermark, run_date)
            if initial_start is None and barrier is None and watermark is None:
                # No prior coverage: an explicit since is the deterministic initial
                # start; otherwise the frozen 60-day backfill applies.
                initial_start = verified_since[0] if verified_since is not None else default_start
                base_start = initial_start
            if verified_since is not None:
                since_day, since_dt = verified_since
                if since_day < base_start:
                    initial_start = min([d for d in (initial_start, since_day) if d])
                    barrier = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                    base_start = since_day
            if initial_start is None:
                initial_start = base_start
        _prewrite_state_if_changed(dd, initial_start, barrier, state, state_tracker)
        window_dates, truncated, follow_up_hint, coverage = _derive_sent_sync_window(
            count, dd, run_date=run_date, since=base_start,
        )
        query_dates = window_dates

    envelopes_total = 0
    truncated_days: list[str] = []
    candidates: list[dict[str, Any]] = []
    failures: list[tuple[str, Exception]] = []
    known_mids, known_eids = _known_index_ids(dd)
    accepted_dates: list[str] = []
    budget = count if count is not None and count > 0 else None
    for date_str in query_dates:
        # Transactional per window: a failing date must not leak the IDs it already
        # marked as known into later windows (that would silently skip those mails).
        attempt_mids, attempt_eids = set(known_mids), set(known_eids)
        try:
            date_candidates, date_stats = _collect_date_candidates(
                date_str, folder=folder, account=account, known_mids=attempt_mids, known_eids=attempt_eids,
                max_pages=max_pages,
            )
        except SentHandoffError:
            raise  # explicit fail-closed window refusal keeps its reason code
        except Exception as error:
            failures.append((date_str, error))
            continue
        if budget is not None and date_candidates and len(candidates) + len(date_candidates) > budget:
            # Item budget reached at a day boundary: never read past it, never
            # advance coverage over it.  The blocked day keeps a concrete recovery.
            truncated = True
            blocked_later = len(query_dates) - len(accepted_dates)
            follow_up_hint = (
                f"Item budget count={count} reached before {date_str}; {blocked_later} later day(s) "
                f"were not read. Watermark advances only over covered days "
                f"({accepted_dates[-1] if accepted_dates else 'unchanged'}). Recovery: "
                f"sync_sent_items_by_dates(dates=['{date_str}'], include_next_day=False) for the "
                "blocked day, or rerun with a higher count."
            )
            coverage.update(
                coverage_end=accepted_dates[-1] if accepted_dates else None,
                covered_days=len(accepted_dates),
                coverage_complete=False,
                next_start=date_str,
            )
            break
        known_mids, known_eids = attempt_mids, attempt_eids
        candidates.extend(date_candidates)
        accepted_dates.append(date_str)
        envelopes_total += int(date_stats.get("envelopes_examined", 0))
        if date_stats.get("truncated"):
            truncated_days.append(date_str)

    if failures:
        # Fail-closed before any write: committing newer successful days would move
        # the watermark past the failed older days and a default retry would never
        # see them again.  Nothing is handed off or indexed.
        failed_dates = ", ".join(date_str for date_str, _ in failures)
        failed_list = ", ".join(f"'{date_str}'" for date_str, _ in failures)
        first_error = failures[0][1]
        raise SentSyncPartialFailure(
            f"Sent sync failed for {len(failures)} date(s): {failed_dates}. "
            f"First error: {type(first_error).__name__}: {first_error}. "
            "Nothing was handed off or indexed (fail-closed); ALL days read in this run were "
            f"discarded, not just {failed_dates}. Recovery: rerun the full original coverage "
            f"start={coverage['coverage_start']} end={coverage['coverage_end']} with "
            f"sync_sent_items(since='{coverage['coverage_start']}') or "
            f"sync_sent_items_by_dates(dates=[{failed_list}], include_next_day=False) only if no "
            "other read day is skipped (the default run re-covers the whole window).",
            {
                "date_windows_synced": len(accepted_dates) + len(failures),
                "envelopes_examined": envelopes_total,
                "new_entries_indexed": 0,
                "new_count": 0,
                "new_items": [],
                "handoff": None,
                "truncated": truncated,
                **coverage,
                "truncated_days": truncated_days,
                "follow_up_hint": follow_up_hint,
            },
            [date_str for date_str, _ in failures],
        )

    committed = _commit_sent_candidates(dd, candidates, account=account)
    new_items = committed["new_items"]
    handoff = committed["handoff"]
    deferred_days = committed.get("deferred_days") or []
    if deferred_days:
        # The batch stopped at 200; never mark the partial last day as covered.
        truncated = True
        first_deferred = deferred_days[0]
        coverage_end = coverage.get("coverage_end")
        covered_days = coverage.get("covered_days", 0)
        try:
            first_dt = datetime.strptime(first_deferred, "%Y-%m-%d")
            start_dt = datetime.strptime(str(coverage.get("coverage_start")), "%Y-%m-%d")
            last_full = first_dt - timedelta(days=1)
            coverage_end = last_full.strftime("%Y-%m-%d") if last_full >= start_dt else None
            covered_days = max(0, (last_full - start_dt).days + 1)
        except (TypeError, ValueError):
            coverage_end = coverage_end if first_deferred == "unparsed" else None
        next_start_value = first_deferred
        if first_deferred == "unparsed":
            # No parseable day: re-read from the window start; the barrier stays put.
            next_start_value = coverage.get("coverage_start")
        coverage.update(
            coverage_end=coverage_end,
            covered_days=covered_days,
            coverage_complete=False,
            next_start=next_start_value,
        )
        follow_up_hint = (
            f"Handoff batch limit {MAX_HANDOFF_ITEMS} reached; {handoff['deferred_count']} item(s) "
            f"remain from {next_start_value}. Consume the pending handoff, then rerun (the default "
            f"window re-reads the barrier) or use "
            f"sync_sent_items_by_dates_report(dates=['{next_start_value}'], include_next_day=False)."
        )
    # Persist the coverage barrier only AFTER the index append: a crash before this
    # point leaves the older barrier behind (safe re-read), never ahead of data.
    try:
        current_state = _read_sync_state(dd)
    except SentHandoffError as error:
        raise SentIndexAppendError(
            "Coverage state unreadable after the index append; the index is committed but the coverage "
            f"barrier is unverified ({error}). A rerun re-reads conservatively."
        ) from error
    current_initial = current_state["initial_start"] or initial_start
    current_barrier = current_state["covered_through"] or barrier
    if dates:
        new_barrier = _advance_days(
            accepted_dates, coverage_start, deferred_days, run_date, current_barrier,
        )
        yesterday = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
        covered_days = 0
        if new_barrier is not None:
            covered_days = max(
                0,
                (datetime.strptime(new_barrier, "%Y-%m-%d")
                 - datetime.strptime(coverage_start, "%Y-%m-%d")).days + 1,
            )
        coverage.update(
            coverage_end=new_barrier,
            covered_days=covered_days,
            coverage_complete=bool(new_barrier is not None and new_barrier >= yesterday),
            next_start=deferred_days[0] if deferred_days else (
                (datetime.strptime(new_barrier, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                if new_barrier is not None else coverage_start
            ),
        )
    elif deferred_days:
        first_deferred = deferred_days[0]
        if first_deferred != "unparsed":
            last_full = (datetime.strptime(first_deferred, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            new_barrier = min([d for d in (current_barrier, last_full) if d]) if current_barrier else last_full
        else:
            new_barrier = current_barrier
    elif truncated:
        new_barrier = coverage.get("coverage_end") or current_barrier
    else:
        new_barrier = coverage.get("coverage_end") or current_barrier
    if not dates and new_barrier is not None:
        # The current day is never final: more mail can arrive there later.  The
        # barrier stops at yesterday so the next run always re-reads today.
        yesterday = (run_date - timedelta(days=1)).strftime("%Y-%m-%d")
        new_barrier = min(new_barrier, yesterday)
    try:
        _write_sync_state(dd, initial_start=current_initial, covered_through=new_barrier)
        if state_tracker is not None:
            state_tracker["state_written"] = True
    except Exception as error:  # noqa: BLE001 - index already committed, barrier unverified
        raise SentIndexAppendError(
            "Coverage state write failed after the index append; the index is committed but the "
            f"coverage barrier is unverified ({type(error).__name__}: {error}). A rerun re-reads from "
            "the previous barrier and merges idempotently by Message-ID."
        ) from error

    result = {
        "date_windows_synced": len(accepted_dates),
        "envelopes_examined": envelopes_total,
        "new_entries_indexed": len(new_items),
        "new_count": len(new_items),
        "new_items": new_items,
        "handoff": handoff,
        "truncated": truncated,
        **coverage,
        **_since_hint(verified_since, coverage.get("coverage_start")),
        "truncated_days": truncated_days,
        "follow_up_hint": follow_up_hint,
    }

    return result


def sync_sent_items(
    count: int | None = 100,
    folder: str = "Sent Items",
    dates: list[str] | None = None,
    account: str | None = None,
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
    since: str | None = None,
    max_pages: int = SENT_SYNC_MAX_PAGES,
) -> dict[str, Any]:
    """Public sync entry; exposes whether only coverage state was written on failure."""
    tracker = {"state_written": False}
    try:
        return _sync_sent_items_impl(
            count=count,
            folder=folder,
            dates=dates,
            account=account,
            data_dir=data_dir,
            workspace_root=workspace_root,
            since=since,
            max_pages=max_pages,
            state_tracker=tracker,
        )
    except Exception as error:
        if tracker["state_written"] and getattr(error, "write_state", None) is None:
            # Precise contract: only the coverage marker/barrier was written; no
            # handoff and no index entry was published by this run.
            try:
                error.write_state = "state"
            except (AttributeError, TypeError):
                pass
        raise


def parse_date_to_datetime(s: str) -> datetime | None:
    """Parse various email/ISO date formats to timezone-naive datetime."""
    if not s:
        return None
    s_str = str(s).strip()
    if len(s_str) >= 10 and s_str[4] == "-" and s_str[7] == "-":
        try:
            return datetime.strptime(s_str[:10], "%Y-%m-%d")
        except Exception:
            pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s_str)
        if dt:
            return dt.replace(tzinfo=None)
    except Exception:
        pass
    return None


def extract_email_address(s: str) -> str:
    """Extract raw email address from header string like 'Name <user@domain.com>'."""
    if not s:
        return ""
    m = re.search(r"[\w\.-]+@[\w\.-]+", s)
    return m.group(0).lower() if m else s.strip().lower()


def check_if_replied(
    email: dict[str, Any],
    sent_lookup: dict[str, Any],
    identity_config: ReplyHeuristicsConfig | None = None,
) -> dict[str, Any] | None:
    """Check whether an incoming email has been replied to according to sent items.

    ``identity_config`` supplies the workspace-bound sent-index identity: the subject
    stopwords for keyword extraction and the sender-domain whitelist for the
    ``domain_to_sender`` heuristic.  ``None`` resolves to the documented MD-ID1
    default identity (dataclass defaults, no catalog file I/O).
    """
    config = identity_config if identity_config is not None else _default_identity_config()
    norm_mid = normalize_message_id(str(email.get("message_id", "")))
    if not norm_mid:
        return None

    # 1. Direct match on in_reply_to (unambiguous technical link, even if subject changed)
    irt_matches = sent_lookup.get("by_in_reply_to", {}).get(norm_mid, [])
    if irt_matches:
        latest = irt_matches[-1]
        return {
            "replied": True,
            "confidence": "high",
            "match_type": "in_reply_to",
            "sent_message_id": latest.get("message_id", ""),
            "sent_date": latest.get("at", ""),
            "sent_envelope_id": latest.get("sent_envelope_id", ""),
            "sent_subject": latest.get("subject", ""),
        }

    # 2. Match on references (unambiguous thread link)
    ref_matches = sent_lookup.get("by_reference", {}).get(norm_mid, [])
    if ref_matches:
        latest = ref_matches[-1]
        return {
            "replied": True,
            "confidence": "high",
            "match_type": "references",
            "sent_message_id": latest.get("message_id", ""),
            "sent_date": latest.get("at", ""),
            "sent_envelope_id": latest.get("sent_envelope_id", ""),
            "sent_subject": latest.get("subject", ""),
        }

    # 3. Clean subject match (heuristically for replies where In-Reply-To was omitted)
    subj = email.get("subject", "")
    if subj:
        cs = clean_subject(subj)
        subj_matches = sent_lookup.get("by_subject_clean", {}).get(cs, [])
        if subj_matches:
            latest = subj_matches[-1]
            return {
                "replied": True,
                "confidence": "high",
                "match_type": "subject_clean",
                "sent_message_id": latest.get("message_id", ""),
                "sent_date": latest.get("at", ""),
                "sent_envelope_id": latest.get("sent_envelope_id", ""),
                "sent_subject": latest.get("subject", ""),
            }

    # 4. Context & Candidate Search:
    # When In-Reply-To is missing and subject changed, check for candidate sent emails
    # to the sender / partner in the same project context within 0-3 days.
    from_raw = email.get("from", "")
    from_addr = extract_email_address(from_raw)
    all_sent = sent_lookup.get("all_entries", [])

    if from_addr and all_sent:
        sender_domain = from_addr.split("@")[-1] if "@" in from_addr else ""
        stopwords = set(config.sent_subject_stopwords)
        domain_whitelist = set(config.sent_sender_domain_whitelist)
        keywords = [w.lower() for w in re.split(r"[\s\-_:/]+", subj) if len(w) >= 4 and w.lower() not in stopwords]

        in_date_raw = str(email.get("date") or email.get("at") or "")
        in_dt = parse_date_to_datetime(in_date_raw)

        best_candidate = None
        for s_entry in all_sent:
            to_field = s_entry.get("to", [])
            if isinstance(to_field, str):
                to_addrs = [extract_email_address(to_field)]
            else:
                to_addrs = [extract_email_address(t) for t in to_field]

            direct_to_sender = from_addr in to_addrs
            domain_to_sender = bool(sender_domain and sender_domain not in domain_whitelist and any(sender_domain in t for t in to_addrs))

            if not (direct_to_sender or domain_to_sender):
                continue

            s_date_raw = str(s_entry.get("at", ""))
            s_dt = parse_date_to_datetime(s_date_raw)

            # Temporal check: Sent email should be on/after incoming email within 30 days
            if in_dt and s_dt:
                delta_days = (s_dt - in_dt).days
                if delta_days < -1 or delta_days > 30:
                    continue
                # If only recipient matched without keywords, require tighter window (<= 3 days)
                if not any(kw in s_entry.get("subject", "").lower() for kw in keywords) and delta_days > 3:
                    continue

            s_subj = s_entry.get("subject", "")
            s_subj_lower = s_subj.lower()
            s_date_str = s_dt.strftime("%Y-%m-%d") if s_dt else s_date_raw[:10]

            matched_kws = [kw for kw in keywords if kw in s_subj_lower]

            if matched_kws or direct_to_sender:
                candidate_confidence = "high" if (direct_to_sender and matched_kws) else "medium"
                best_candidate = {
                    "sent_message_id": s_entry.get("message_id", ""),
                    "sent_date": s_entry.get("at", ""),
                    "sent_envelope_id": s_entry.get("sent_envelope_id", ""),
                    "sent_subject": s_subj,
                    "matched_recipient": from_addr if direct_to_sender else to_addrs[0],
                    "matched_keywords": matched_kws,
                    "confidence": candidate_confidence,
                    "reason": f"Gesendet an {from_addr} am {s_date_str} mit Betreff '{s_subj}' (Keywords: {', '.join(matched_kws) if matched_kws else 'Empfänger-Match'})",
                }
                if candidate_confidence == "high":
                    break

        if best_candidate:
            return {
                "replied": False,
                "has_candidate": True,
                "confidence": best_candidate["confidence"],
                "candidate": best_candidate,
            }

    return None


def auto_resolve_replies_from_sent(
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Audit replies-needed.jsonl against sent-index.jsonl and resolve answered cases.

    ``workspace_root`` loads the workspace desk-signals identity catalog once and
    threads it into every candidate search; ``None`` keeps the documented default
    identity.
    """
    from .action_log import resolve_case

    dd = data_dir or resolve_data_dir()
    sent_idx = load_sent_index(dd)
    identity_config = (
        load_reply_heuristics(workspace_root) if workspace_root is not None else None
    )
    rn_path = dd / "replies-needed.jsonl"

    if not rn_path.exists():
        return {
            "ok": True,
            "total_checked": 0,
            "resolved_count": 0,
            "resolved_items": [],
            "remaining_open": [],
        }

    with rn_path.open("r", encoding="utf-8") as f:
        entries = [json.loads(line.strip()) for line in f if line.strip()]

    resolved_items: list[dict[str, Any]] = []
    remaining_open: list[dict[str, Any]] = []
    updated_entries: list[dict[str, Any]] = []
    needs_rewrite = False

    for entry in entries:
        reply_info = check_if_replied(entry, sent_idx, identity_config=identity_config)
        if reply_info and reply_info.get("replied"):
            mid = normalize_message_id(entry.get("message_id", ""))
            resolve_case(
                data_dir=dd,
                message_id=mid,
                status="replied",
                resolution=f"Beantwortet via Sent Items: {reply_info.get('sent_subject', '')}",
                resolved_by=reply_info.get("sent_message_id"),
            )
            resolved_items.append({
                "message_id": mid,
                "subject": entry.get("subject", ""),
                "sent_message_id": reply_info.get("sent_message_id"),
                "sent_subject": reply_info.get("sent_subject"),
                "status": "replied",
            })
            needs_rewrite = True
        else:
            cand = reply_info.get("candidate") if (reply_info and reply_info.get("has_candidate")) else None
            if entry.get("reply_candidate") != cand:
                if cand:
                    entry["reply_candidate"] = cand
                else:
                    entry.pop("reply_candidate", None)
                needs_rewrite = True
            updated_entries.append(entry)
            remaining_open.append({
                "message_id": entry.get("message_id"),
                "subject": entry.get("subject"),
                "from": entry.get("from"),
                "reply_candidate": entry.get("reply_candidate"),
            })

    if needs_rewrite:
        atomic_rewrite_jsonl(rn_path, updated_entries)

    return {
        "ok": True,
        "total_checked": len(entries),
        "resolved_count": len(resolved_items),
        "resolved_items": resolved_items,
        "remaining_open_count": len(remaining_open),
        "remaining_open": remaining_open,
    }
