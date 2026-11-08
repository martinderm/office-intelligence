"""Core package for skills/calendar-desk.

Provides RFC-5545 iCalendar codec, timezone resolution, recurrence expansion,
untrusted calendar content fencing, backend config loader, CalDAV client transport,
Free/Busy / slot finding engine, and two-phase write engine without external dependencies.
"""

from __future__ import annotations

from .backend_config import (
    AccountCredentials,
    CalDAVConfigError,
    load_backend_config,
    resolve_account_credentials,
    resolve_calendar_target,
)
from .caldav_client import (
    CalDAVClient,
    CalDAVProtocolError,
    parse_dav_xml,
)
from .freebusy import (
    compute_freebusy,
    find_free_slots,
)
from .ical_codec import (
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
from .injection_fence import (
    ESCAPED_TAG_PLACEHOLDER,
    FENCE_TAG,
    FENCE_VERSION,
    TEXT_FIELDS_TO_FENCE,
    fence_calendar_record,
    fence_calendar_text,
    sanitize_fence_collisions,
    scan_calendar_text,
)
from .write_engine import (
    ENV_STD_LOCK_ROOT,
    CalendarWriteError,
    _load_guard,
    _trusted_lock_root,
    apply_calendar_write,
    canonical_json_sha256,
    compute_plan_sha256,
    inspect_workspace,
    plan_calendar_write,
    recover_workspace,
    require_owned_workspace_lock,
    respond_invitation,
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
    "AccountCredentials",
    "CalDAVConfigError",
    "load_backend_config",
    "resolve_account_credentials",
    "resolve_calendar_target",
    "CalDAVClient",
    "CalDAVProtocolError",
    "parse_dav_xml",
    "compute_freebusy",
    "find_free_slots",
    "ENV_STD_LOCK_ROOT",
    "CalendarWriteError",
    "_load_guard",
    "_trusted_lock_root",
    "apply_calendar_write",
    "canonical_json_sha256",
    "compute_plan_sha256",
    "inspect_workspace",
    "plan_calendar_write",
    "recover_workspace",
    "require_owned_workspace_lock",
    "respond_invitation",
]
