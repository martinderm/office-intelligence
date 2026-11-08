"""Free/Busy computation and slot finding engine for skills/calendar-desk.

Implements deterministic interval merging, conflict detection, IANA timezone
working-hours evaluation, buffer padding, and termino-api slot generation.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from .caldav_client import CalDAVProtocolError
from .ical_codec import expand_record_occurrences, resolve_tzid
from .injection_fence import FENCE_TAG, fence_calendar_text, sanitize_fence_collisions


def fence_text(text: str) -> str:
    """Fence text with <untrusted_calendar_content> tags, preventing duplicate fencing."""
    raw = str(text or "")
    close_tag = f"</{FENCE_TAG}>"
    tag_prefix = f"<{FENCE_TAG}"
    is_fenced = (
        raw.startswith(f"<{FENCE_TAG}>")
        or (
            raw.startswith(tag_prefix)
            and len(raw) > len(tag_prefix)
            and (raw[len(tag_prefix)] == ">" or raw[len(tag_prefix)].isspace())
        )
    ) and raw.endswith(close_tag)

    if is_fenced:
        gt_pos = raw.find(">")
        if gt_pos != -1 and gt_pos <= len(raw) - len(close_tag):
            inner = raw[gt_pos + 1 : -len(close_tag)]
            if inner.startswith("\n"):
                inner = inner[1:]
            if inner.endswith("\n"):
                inner = inner[:-1]
            return fence_calendar_text(inner)["fenced"]

    return fence_calendar_text(raw)["fenced"]


def compute_freebusy(
    records: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    window_start_utc: str,
    window_end_utc: str,
    *,
    tzid: str = "Europe/Vienna",
    skipped: Optional[Sequence[Mapping[str, Any]]] = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Compute busy intervals, merged intervals, and event conflicts within search window."""
    if isinstance(records, Mapping):
        skipped = skipped if skipped is not None else records.get("skipped")
        truncated = bool(truncated or records.get("truncated"))
        records = records.get("records", [])

    if skipped or truncated:
        raise CalDAVProtocolError(
            "CALDAV_INCOMPLETE_FREEBUSY",
            f"Cannot compute reliable free/busy from incomplete calendar data (skipped={len(skipped or [])}, truncated={bool(truncated)}).",
        )

    default_tz = resolve_tzid(tzid)

    win_start_dt = datetime.fromisoformat(window_start_utc.replace("Z", "+00:00"))
    if win_start_dt.tzinfo is None:
        win_start_dt = win_start_dt.replace(tzinfo=timezone.utc)
    win_end_dt = datetime.fromisoformat(window_end_utc.replace("Z", "+00:00"))
    if win_end_dt.tzinfo is None:
        win_end_dt = win_end_dt.replace(tzinfo=timezone.utc)

    # 1. Expand recurrences if any record is recurring
    expanded_records: list[dict[str, Any]] = []
    # Separate recurring masters and overrides
    masters: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for r in records:
        r_dict = dict(r)
        if not r_dict.get("recurrence_id") and not r_dict.get("is_occurrence") and r_dict.get("rrule"):
            masters.append(r_dict)
        elif r_dict.get("recurrence_id") and not r_dict.get("is_occurrence"):
            overrides.append(r_dict)
        else:
            expanded_records.append(r_dict)

    for m in masters:
        matching_ovrs = [
            ovr for ovr in overrides if ovr.get("uid") == m.get("uid")
        ]
        expanded = expand_record_occurrences(
            m,
            window_start_utc,
            window_end_utc,
            override_records=matching_ovrs,
        )
        expanded_records.extend(expanded)

    # Any overrides that didn't have a matching master in masters
    master_uids = {m.get("uid") for m in masters}
    for ovr in overrides:
        if ovr.get("uid") not in master_uids:
            expanded_records.append(ovr)

    # 2. Filter busy intervals
    busy_list: list[dict[str, Any]] = []
    for rec in expanded_records:
        if rec.get("status") == "CANCELLED":
            continue
        if rec.get("transp") == "TRANSPARENT":
            continue

        start_info = rec.get("start", {})
        end_info = rec.get("end", {})

        is_all_day = bool(start_info.get("all_day"))
        if is_all_day:
            st_date_str = start_info.get("date")
            end_date_str = end_info.get("date") or st_date_str
            if not st_date_str:
                continue
            st_d = date.fromisoformat(st_date_str)
            end_d = date.fromisoformat(end_date_str)
            if end_d <= st_d:
                end_d = st_d + timedelta(days=1)
            rec_tz = resolve_tzid(start_info.get("tzid") or tzid)
            st_dt = datetime(st_d.year, st_d.month, st_d.day, 0, 0, 0, tzinfo=rec_tz).astimezone(timezone.utc)
            end_dt = datetime(end_d.year, end_d.month, end_d.day, 0, 0, 0, tzinfo=rec_tz).astimezone(timezone.utc)
        else:
            st_utc_str = start_info.get("utc")
            end_utc_str = end_info.get("utc")
            if not st_utc_str or not end_utc_str:
                continue
            st_dt = datetime.fromisoformat(st_utc_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_utc_str.replace("Z", "+00:00"))

        if st_dt.tzinfo is None:
            st_dt = st_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        # Overlap check with window
        if end_dt <= win_start_dt or st_dt >= win_end_dt:
            continue

        clamped_st = max(st_dt, win_start_dt)
        clamped_end = min(end_dt, win_end_dt)
        if clamped_end <= clamped_st:
            continue
        dur_mins = int((clamped_end - clamped_st).total_seconds() // 60)

        busy_list.append(
            {
                "uid": rec.get("uid", ""),
                "summary": fence_text(rec.get("summary", "")),
                "start_utc": clamped_st.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_utc": clamped_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_minutes": dur_mins,
                "_st_dt": clamped_st,
                "_end_dt": clamped_end,
            }
        )

    # 3. Sort intervals chronologically
    busy_list.sort(key=lambda x: (x["_st_dt"], x["_end_dt"]))

    # 4. Conflict detection
    conflicts: list[dict[str, Any]] = []
    n = len(busy_list)
    for i in range(n):
        for j in range(i + 1, n):
            ev_a = busy_list[i]
            ev_b = busy_list[j]
            # If b starts after a ends, no overlap with subsequent either (since sorted by start)
            if ev_b["_st_dt"] >= ev_a["_end_dt"]:
                break
            # True overlap (strictly positive duration)
            if ev_a["_end_dt"] > ev_b["_st_dt"] and ev_a["_st_dt"] < ev_b["_end_dt"]:
                overlap_st = max(ev_a["_st_dt"], ev_b["_st_dt"])
                overlap_end = min(ev_a["_end_dt"], ev_b["_end_dt"])
                conflicts.append(
                    {
                        "event_a": ev_a["uid"],
                        "event_b": ev_b["uid"],
                        "overlap_start_utc": overlap_st.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "overlap_end_utc": overlap_end.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    }
                )

    # 5. Merged intervals
    merged_busy: list[dict[str, Any]] = []
    for item in busy_list:
        st = item["_st_dt"]
        end = item["_end_dt"]
        if not merged_busy:
            merged_busy.append(
                {
                    "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "duration_minutes": int((end - st).total_seconds() // 60),
                    "_st_dt": st,
                    "_end_dt": end,
                }
            )
        else:
            prev = merged_busy[-1]
            if st <= prev["_end_dt"]:
                # Overlapping or adjacent
                if end > prev["_end_dt"]:
                    prev["_end_dt"] = end
                    prev["end_utc"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
                    prev["duration_minutes"] = int(
                        (end - prev["_st_dt"]).total_seconds() // 60
                    )
            else:
                merged_busy.append(
                    {
                        "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "duration_minutes": int((end - st).total_seconds() // 60),
                        "_st_dt": st,
                        "_end_dt": end,
                    }
                )

    # Cleanup internal datetime keys
    for b in busy_list:
        b.pop("_st_dt", None)
        b.pop("_end_dt", None)
    for m in merged_busy:
        m.pop("_st_dt", None)
        m.pop("_end_dt", None)

    total_busy = sum(item["duration_minutes"] for item in merged_busy)
    return {
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "busy_intervals": busy_list,
        "merged_busy": merged_busy,
        "conflicts": conflicts,
        "total_busy_minutes": total_busy,
    }


def _add_free_interval(
    free_st: datetime,
    free_end: datetime,
    tz: Any,
    min_slot_minutes: int,
    slot_minutes: Optional[int],
    max_slots: Optional[int],
    slots: list[dict[str, Any]],
    termino_slots: list[str],
) -> bool:
    """Add slot(s) for a free interval. Returns True if max_slots limit has been reached."""
    dur = int((free_end - free_st).total_seconds() // 60)
    if dur < min_slot_minutes:
        return False

    if slot_minutes is not None and slot_minutes > 0:
        step = timedelta(minutes=slot_minutes)
        ptr = free_st
        while ptr + step <= free_end:
            if max_slots is not None and len(slots) >= max_slots:
                return True
            _append_slot(ptr, ptr + step, slot_minutes, tz, slots, termino_slots)
            ptr += step
            if max_slots is not None and len(slots) >= max_slots:
                return True
    else:
        if max_slots is not None and len(slots) >= max_slots:
            return True
        _append_slot(free_st, free_end, dur, tz, slots, termino_slots)
        if max_slots is not None and len(slots) >= max_slots:
            return True

    return False


def find_free_slots(
    records: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    window_start_utc: str,
    window_end_utc: str,
    *,
    tzid: str = "Europe/Vienna",
    working_hours: str = "09:00-17:00",
    min_slot_minutes: int = 60,
    buffer_minutes: int = 0,
    weekdays_only: bool = True,
    slot_minutes: Optional[int] = None,
    max_slots: Optional[int] = None,
    skipped: Optional[Sequence[Mapping[str, Any]]] = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Find available meeting slots within working hours in specified timezone."""
    fb_res = compute_freebusy(
        records,
        window_start_utc,
        window_end_utc,
        tzid=tzid,
        skipped=skipped,
        truncated=truncated,
    )
    merged_busy = fb_res["merged_busy"]

    tz = resolve_tzid(tzid)

    # Parse working hours: "HH:MM-HH:MM"
    try:
        wh_parts = working_hours.split("-")
        st_parts = [int(p) for p in wh_parts[0].split(":")]
        end_parts = [int(p) for p in wh_parts[1].split(":")]
        wh_start_t = time(st_parts[0], st_parts[1])
        wh_end_t = time(end_parts[0], end_parts[1])
    except Exception as exc:
        raise ValueError(
            f"Invalid working_hours format: {working_hours!r}; expected 'HH:MM-HH:MM'"
        ) from exc

    win_start_dt = datetime.fromisoformat(window_start_utc.replace("Z", "+00:00"))
    if win_start_dt.tzinfo is None:
        win_start_dt = win_start_dt.replace(tzinfo=timezone.utc)
    win_end_dt = datetime.fromisoformat(window_end_utc.replace("Z", "+00:00"))
    if win_end_dt.tzinfo is None:
        win_end_dt = win_end_dt.replace(tzinfo=timezone.utc)

    start_local = win_start_dt.astimezone(tz)
    end_local = win_end_dt.astimezone(tz)

    slots: list[dict[str, Any]] = []
    termino_slots: list[str] = []

    curr_date = start_local.date()
    end_date = end_local.date()

    while curr_date <= end_date:
        # Check weekdays only
        if weekdays_only and curr_date.weekday() >= 5:
            curr_date += timedelta(days=1)
            continue

        work_st_local = datetime.combine(curr_date, wh_start_t, tzinfo=tz)
        work_end_local = datetime.combine(curr_date, wh_end_t, tzinfo=tz)

        work_st_utc = work_st_local.astimezone(timezone.utc)
        work_end_utc = work_end_local.astimezone(timezone.utc)

        day_st_utc = max(work_st_utc, win_start_dt)
        day_end_utc = min(work_end_utc, win_end_dt)

        if day_st_utc >= day_end_utc:
            curr_date += timedelta(days=1)
            continue

        # Collect padded busy intervals for this day
        day_busy_intervals: list[tuple[datetime, datetime]] = []
        for b in merged_busy:
            b_st = datetime.fromisoformat(
                b["start_utc"].replace("Z", "+00:00")
            ) - timedelta(minutes=buffer_minutes)
            b_end = datetime.fromisoformat(
                b["end_utc"].replace("Z", "+00:00")
            ) + timedelta(minutes=buffer_minutes)

            if b_end <= day_st_utc or b_st >= day_end_utc:
                continue
            day_busy_intervals.append(
                (max(b_st, day_st_utc), min(b_end, day_end_utc))
            )

        # Merge day's padded busy intervals
        day_busy_intervals.sort()
        merged_day_busy: list[tuple[datetime, datetime]] = []
        for b_st, b_end in day_busy_intervals:
            if not merged_day_busy:
                merged_day_busy.append((b_st, b_end))
            else:
                prev_st, prev_end = merged_day_busy[-1]
                if b_st <= prev_end:
                    merged_day_busy[-1] = (prev_st, max(prev_end, b_end))
                else:
                    merged_day_busy.append((b_st, b_end))

        # Invert busy into free intervals
        curr_ptr = day_st_utc
        stop_all = False
        for b_st, b_end in merged_day_busy:
            if b_st > curr_ptr:
                if _add_free_interval(
                    curr_ptr, b_st, tz, min_slot_minutes, slot_minutes, max_slots, slots, termino_slots
                ):
                    stop_all = True
                    break
            curr_ptr = max(curr_ptr, b_end)

        if stop_all or (max_slots is not None and len(slots) >= max_slots):
            break

        if day_end_utc > curr_ptr:
            if _add_free_interval(
                curr_ptr, day_end_utc, tz, min_slot_minutes, slot_minutes, max_slots, slots, termino_slots
            ):
                break

        if max_slots is not None and len(slots) >= max_slots:
            break

        curr_date += timedelta(days=1)

    total_free = sum(s["duration_minutes"] for s in slots)
    return {
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "tzid": tzid,
        "working_hours": working_hours,
        "min_slot_minutes": min_slot_minutes,
        "slot_minutes": slot_minutes,
        "max_slots": max_slots,
        "buffer_minutes": buffer_minutes,
        "slots": slots,
        "termino_slots": termino_slots,
        "total_free_minutes": total_free,
    }


def _append_slot(
    slot_st: datetime,
    slot_end: datetime,
    dur: int,
    tz: Any,
    slots: list[dict[str, Any]],
    termino_slots: list[str],
) -> None:
    st_loc = slot_st.astimezone(tz)
    end_loc = slot_end.astimezone(tz)
    date_str = st_loc.strftime("%Y-%m-%d")
    st_time_str = st_loc.strftime("%H:%M")
    end_time_str = end_loc.strftime("%H:%M")
    termino_str = f"{date_str}|{st_time_str}->{end_time_str}"

    slots.append(
        {
            "start_utc": slot_st.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": slot_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_local": st_loc.isoformat(),
            "end_local": end_loc.isoformat(),
            "date": date_str,
            "duration_minutes": dur,
        }
    )
    termino_slots.append(termino_str)
