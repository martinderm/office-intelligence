"""Sent items indexer and thread response matcher."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import atomic_rewrite_jsonl, normalize_message_id, utc_now_iso, resolve_data_dir
from .himalaya import run_himalaya, get_single_email_details
from .sent_handoff import (
    HANDOFF_FILENAME,
    MAX_HANDOFF_ITEMS,
    SentHandoffError,
    SentHandoffPublishError,
    SentIndexAppendError,
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
    """Oldest first; undated last; envelope id as the deterministic tie-break."""
    entry = candidate["entry"]
    parsed = parse_date_to_datetime(str(entry.get("at", "")))
    eid = str(entry.get("sent_envelope_id", ""))
    return (parsed is None, parsed or datetime.min, int(eid) if eid.isdigit() else -1, eid)


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
    pending items of another mailbox, pending items plus further new items
    (``handoff_pending``), and more new items than one handoff may hold
    (``handoff_overflow``).  No reachability is assumed: an overflow is refused as a
    whole before any write, so no surplus can silently fall out of a later window.
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
            "Consume the pending items (ledger) and rerun.",
            {
                "pending_count": len(pending),
                "blocked_new_count": len(extra),
                "consumed_ignored": consumed_ignored,
                "state": "pending",
            },
        )

    # No reachability guess: more new items than one handoff may hold is always
    # refused before any write, so a surplus can never fall out of a later window.
    if len(extra) > MAX_HANDOFF_ITEMS:
        per_day: dict[str, int] = {}
        for candidate in extra:
            parsed = parse_date_to_datetime(str(candidate["entry"].get("at", "")))
            day = parsed.strftime("%Y-%m-%d") if parsed else "unparsed"
            per_day[day] = per_day.get(day, 0) + 1
        raise SentHandoffError(
            "handoff_overflow",
            f"{len(extra)} new Sent Items exceed one handoff ({MAX_HANDOFF_ITEMS}); nothing was handed "
            "off or indexed. Recover via the Python API sync_sent_items(dates=[...]) / "
            "sync_sent_items_by_dates(..., include_next_day=False), oldest days first, and consume the "
            "handoff in between; the --sync-sent count/CLI is not a date-range recovery.",
            {
                "candidate_count": len(extra),
                "max_items": MAX_HANDOFF_ITEMS,
                "candidates_per_day": dict(sorted(per_day.items())[:60]),
                "state": state_before,
            },
        )

    committed = ordered
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
            # Kept for envelope compatibility: nothing is deferred any more, an
            # overflow is refused as a whole (``handoff_overflow``).
            "deferred_count": 0,
            "headers_incomplete_count": sum(1 for item in new_items if not item["headers_complete"]),
            "ledger_torn_tail": ledger["torn_tail"],
            "max_items": MAX_HANDOFF_ITEMS,
        },
    }


def _sync_sent_items_for_date(
    date_str: str,
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
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
        date_str, folder=folder, account=account, known_mids=known_mids, known_eids=known_eids
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read one Sent-Items date (no writes) and return the not-yet-indexed candidates.

    ``known_mids``/``known_eids`` are updated in place so overlapping dates of one
    run never yield the same message twice.  Returns ``(candidates, stats)`` with
    ``stats = {"envelopes_examined", "truncated"}``.
    """
    if account is None or not str(account).strip():
        raise ValueError(
            "Missing account: cannot bind sent-index entries to a mailbox without "
            "a verified account. Bind an account before syncing Sent Items."
        )

    # Date query via Himalaya (fast single-day filter); transport/parse errors
    # are fail-closed and never collapse into a silent "0 new mails".
    try:
        out = run_himalaya(
            ["-o", "json", "envelope", "list", "-f", folder, "-s", str(SENT_SYNC_PAGE_SIZE), f"date {date_str}"],
            account=account,
            timeout=30,
        )
        if "[" in out:
            out = out[out.find("["):]
        envs = json.loads(out)
    except Exception as error:
        raise RuntimeError(
            f"Sent sync failed for {date_str}: {type(error).__name__}: {error}"
        ) from error

    if not isinstance(envs, list):
        raise RuntimeError(
            f"Sent sync failed for {date_str}: Himalaya returned a non-list envelope payload."
        )

    envelopes_examined = len(envs)
    # A full page may hide further envelopes; flag instead of silently truncating.
    truncated = envelopes_examined >= SENT_SYNC_PAGE_SIZE
    stats = {"envelopes_examined": envelopes_examined, "truncated": truncated}
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
) -> int:
    """Fetch envelopes for a specific date (YYYY-MM-DD) from Sent Items and stream into sent-index.jsonl.

    Returns the number of newly indexed entries.  Fails closed with a
    ``RuntimeError`` naming the date on any Himalaya/parse failure (never a
    silent ``0``), and fails loud with a ``ValueError`` before any file write if
    ``account`` is missing: every indexed entry must be bound to the verified
    workspace account rather than a hardcoded mailbox.
    """
    return int(_sync_sent_items_for_date(date_str, folder=folder, account=account, data_dir=data_dir)["added"])


def _dedupe_iso_dates(dates: list[str]) -> list[str]:
    """Normalize a date list to sorted unique ISO ``YYYY-MM-DD`` strings."""
    unique_dates: set[str] = set()
    for d in dates:
        d_clean = str(d).strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", d_clean):
            unique_dates.add(d_clean)
    return sorted(unique_dates)


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


def sync_sent_items_by_dates(
    dates: list[str],
    folder: str = "Sent Items",
    account: str | None = None,
    data_dir: Path | None = None,
    include_next_day: bool = True,
) -> int:
    """Sync Sent Items for a list of ISO dates (e.g. ['2026-01-19', '2026-01-20']).

    All dates are read first and committed **once** (one handoff, then one index
    append).  A commit per date would let the second date collide with the unconsumed
    handoff written for the first one (``handoff_pending``).  Returns the number of
    newly indexed entries; a failing date raises before any write.
    """
    dd = data_dir or resolve_data_dir()
    known_mids, known_eids = _known_index_ids(dd) if account and str(account).strip() else (set(), set())
    candidates: list[dict[str, Any]] = []
    for d_str in _expand_sync_dates(dates, include_next_day=include_next_day):
        # Same transactional rule as sync_sent_items: a failing date discards its
        # own ID marks so no later window silently skips a mail.
        attempt_mids, attempt_eids = set(known_mids), set(known_eids)
        date_candidates, _ = _collect_date_candidates(
            d_str, folder=folder, account=account, known_mids=attempt_mids, known_eids=attempt_eids
        )
        known_mids, known_eids = attempt_mids, attempt_eids
        candidates.extend(date_candidates)
    if not candidates:
        return 0
    return len(_commit_sent_candidates(dd, candidates, account=account)["new_items"])


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


def _derive_sent_sync_window(count: int | None, data_dir: Path) -> tuple[list[str], str | None]:
    """Derive the no-dates sync window from the sent-index watermark.

    The window spans from the newest indexed sent date (watermark) through today
    inclusive; an empty index yields today only.  It is capped at
    ``MAX_SYNC_WINDOW_DAYS`` per run and bounded to the most recent ``count``
    days when the gap exceeds ``count`` (today is always included).  When either
    bound trims earlier gap days, the returned follow-up hint describes the
    *final* end state: it names every bound that applied and counts the days
    still unsynced from the final ``start``, so telemetry never overstates the
    synced span (FR-26/MD-SE2).  ``start`` is resolved after both bounds before
    the hint is built.
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    watermark = _sent_index_watermark(data_dir)
    if watermark is None:
        start = today
    else:
        start = datetime(watermark.year, watermark.month, watermark.day)
    if start > today:
        start = today

    original_start = start
    gap_days = (today - start).days + 1

    cap_applied = gap_days > MAX_SYNC_WINDOW_DAYS
    if cap_applied:
        start = today - timedelta(days=MAX_SYNC_WINDOW_DAYS - 1)

    count_applied = False
    if count is not None and count > 0 and (today - start).days + 1 > count:
        start = today - timedelta(days=count - 1)
        count_applied = True

    window_days = (today - start).days + 1
    # Days between the watermark and the final start are the ones either bound
    # left unsynced; compute them from the FINAL start, never the pre-bound one.
    unsynced_days = (start - original_start).days

    follow_up_hint: str | None = None
    if unsynced_days > 0:
        applied_bounds: list[str] = []
        if cap_applied:
            applied_bounds.append(f"MAX_SYNC_WINDOW_DAYS={MAX_SYNC_WINDOW_DAYS} cap")
        if count_applied:
            applied_bounds.append(f"count={count} bound")
        follow_up_hint = (
            f"Sent-index watermark gap of {gap_days} days; synced the most recent "
            f"{window_days} day(s) ({' and '.join(applied_bounds)}). "
            f"{unsynced_days} earlier day(s) remain unsynced (watermark gap)."
        )

    window = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(window_days)]
    return window, follow_up_hint


def sync_sent_items(
    count: int | None = 100,
    folder: str = "Sent Items",
    dates: list[str] | None = None,
    account: str | None = None,
    data_dir: Path | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Sync Sent Items by explicit dates or by an index-watermark-derived window.

    The no-dates path queries every day from the sent-index watermark through
    today (capped at ``MAX_SYNC_WINDOW_DAYS``, optionally bounded to the most
    recent ``count`` days) instead of a hardwired 7-day window.  Per-date
    Himalaya/parse failures are collected and re-raised fail-closed with the
    failing dates named, so the caller never sees a silent ``ok``.

    The date windows are read first (no writes); the new items are then handed
    off to ``new-sent-items.json`` *before* the sent-index grows (see
    ``_commit_sent_candidates``).  Detection is relative to the previous index
    state, so a repeated run yields no new items and no duplicate handoff.

    Returns ``{"date_windows_synced", "envelopes_examined",
    "new_entries_indexed", "new_count", "new_items", "handoff",
    "truncated_days", "follow_up_hint"}``.  ``SentHandoffError`` (pending/invalid
    handoff) is raised before any write; date failures raise
    ``SentSyncPartialFailure`` after the successful dates were committed.
    """
    dd = data_dir or resolve_data_dir()
    follow_up_hint: str | None = None
    if dates:
        window_dates = _dedupe_iso_dates(dates)
    else:
        window_dates, follow_up_hint = _derive_sent_sync_window(count, dd)

    query_dates = _expand_sync_dates(window_dates, include_next_day=True)
    envelopes_total = 0
    truncated_days: list[str] = []
    candidates: list[dict[str, Any]] = []
    failures: list[tuple[str, Exception]] = []
    known_mids, known_eids = _known_index_ids(dd)
    for date_str in query_dates:
        # Transactional per window: a failing date must not leak the IDs it already
        # marked as known into later windows (that would silently skip those mails).
        attempt_mids, attempt_eids = set(known_mids), set(known_eids)
        try:
            date_candidates, date_stats = _collect_date_candidates(
                date_str, folder=folder, account=account, known_mids=attempt_mids, known_eids=attempt_eids
            )
        except Exception as error:
            failures.append((date_str, error))
            continue
        known_mids, known_eids = attempt_mids, attempt_eids
        candidates.extend(date_candidates)
        envelopes_total += int(date_stats.get("envelopes_examined", 0))
        if date_stats.get("truncated"):
            truncated_days.append(date_str)

    committed = _commit_sent_candidates(dd, candidates, account=account)
    new_items = committed["new_items"]
    handoff = committed["handoff"]

    result = {
        "date_windows_synced": len(query_dates),
        "envelopes_examined": envelopes_total,
        "new_entries_indexed": len(new_items),
        "new_count": len(new_items),
        "new_items": new_items,
        "handoff": handoff,
        "truncated_days": truncated_days,
        "follow_up_hint": follow_up_hint,
    }

    if failures:
        failed_dates = ", ".join(date_str for date_str, _ in failures)
        first_error = failures[0][1]
        raise SentSyncPartialFailure(
            f"Sent sync failed for {len(failures)} date(s): {failed_dates}. "
            f"First error: {type(first_error).__name__}: {first_error}",
            result,
            [date_str for date_str, _ in failures],
        ) from first_error

    return result


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
