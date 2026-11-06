"""Core package for skills/calendar-desk.

Provides RFC-5545 iCalendar codec, timezone resolution, recurrence expansion,
and untrusted calendar content fencing without external dependencies.
"""

from __future__ import annotations

from core.ical_codec import (
    ICalCodecError,
    WINDOWS_TZ_ALIASES,
    build_vevent_ics,
    escape_ics_text,
    expand_record_occurrences,
    extract_calendar_object_ref,
    fold_ics_line,
    parse_ics_to_records,
    resolve_tzid,
    unescape_ics_text,
    unfold_ics_lines,
    update_vevent_ics,
)
from core.injection_fence import (
    ESCAPED_TAG_PLACEHOLDER,
    FENCE_TAG,
    FENCE_VERSION,
    TEXT_FIELDS_TO_FENCE,
    fence_calendar_record,
    fence_calendar_text,
    sanitize_fence_collisions,
    scan_calendar_text,
)

__all__ = [
    "ICalCodecError",
    "WINDOWS_TZ_ALIASES",
    "build_vevent_ics",
    "escape_ics_text",
    "expand_record_occurrences",
    "extract_calendar_object_ref",
    "fold_ics_line",
    "parse_ics_to_records",
    "resolve_tzid",
    "unescape_ics_text",
    "unfold_ics_lines",
    "update_vevent_ics",
    "ESCAPED_TAG_PLACEHOLDER",
    "FENCE_TAG",
    "FENCE_VERSION",
    "TEXT_FIELDS_TO_FENCE",
    "fence_calendar_record",
    "fence_calendar_text",
    "sanitize_fence_collisions",
    "scan_calendar_text",
]
