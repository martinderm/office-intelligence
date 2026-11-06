"""Autonomous stdlib-only calendar extraction helper for mail-desk attachments.

Part of Ticket CD-4 (mail-desk .ics bridge).
Provides CalendarExtractError and extract_calendar_objects with exact
4-key schema matching event_dossier:
{"uid", "sequence", "recurrence_id", "content_sha256"}
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Optional
from zoneinfo import ZoneInfo

MAX_ICS_BYTES = 1_048_576  # 1 MiB

WINDOWS_TZ_ALIASES: dict[str, str] = {
    "W. Europe Standard Time": "Europe/Vienna",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
    "UTC": "UTC",
}


class CalendarExtractError(ValueError):
    """Raised when an .ics calendar payload fails fail-closed validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unfold_lines(raw_ics: str) -> list[str]:
    """Unfold RFC 5545 lines by merging continuation lines starting with space or tab."""
    lines: list[str] = []
    for raw_line in raw_ics.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw_line:
            continue
        if raw_line[0] in (" ", "\t") and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    return lines


def _parse_prop_line(line: str) -> tuple[str, dict[str, str], str]:
    """Parse a single logical line into property name, parameter dict, and raw value."""
    in_quotes = False
    colon_idx = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon_idx = i
            break
    if colon_idx == -1:
        return line.strip().upper(), {}, ""

    prop_part = line[:colon_idx]
    value_part = line[colon_idx + 1:]

    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in prop_part:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == ";" and not in_quotes:
            tokens.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))

    prop_name = tokens[0].upper().strip()
    params: dict[str, str] = {}
    for param_str in tokens[1:]:
        if "=" in param_str:
            p_name, p_val = param_str.split("=", 1)
            params[p_name.strip().upper()] = p_val.strip().strip('"')

    return prop_name, params, value_part


def _parse_recurrence_id(dt_str: str, params: dict[str, str], default_tz: str = "Europe/Vienna") -> str:
    """Parse RECURRENCE-ID into a normalized UTC ISO-8601 or DATE string."""
    clean_dt = dt_str.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or (len(clean_dt) == 8 and clean_dt.isdigit())

    if is_date:
        try:
            parsed_date = datetime.strptime(clean_dt, "%Y%m%d").date()
            return parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            return clean_dt

    raw_tz = params.get("TZID")
    if raw_tz:
        clean_tz = raw_tz.strip().strip('"').strip("'")
        tz_target = WINDOWS_TZ_ALIASES.get(clean_tz, clean_tz)
    else:
        clean_default = default_tz.strip().strip('"').strip("'")
        tz_target = WINDOWS_TZ_ALIASES.get(clean_default, clean_default)

    try:
        tz = ZoneInfo(tz_target)
    except Exception:
        try:
            clean_default = default_tz.strip().strip('"').strip("'")
            fallback_tz = WINDOWS_TZ_ALIASES.get(clean_default, clean_default)
            tz = ZoneInfo(fallback_tz)
        except Exception:
            tz = timezone.utc

    try:
        if clean_dt.endswith("Z"):
            dt_utc = datetime.strptime(clean_dt, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            naive_dt = datetime.strptime(clean_dt, "%Y%m%dT%H%M%S")
            dt_local = naive_dt.replace(tzinfo=tz)
            dt_utc = dt_local.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return clean_dt


def extract_calendar_objects(ics_text: str) -> list[dict[str, Any]]:
    """Extract standard calendar_objects handoff items from an .ics calendar payload."""
    if not ics_text or not ics_text.strip():
        raise CalendarExtractError("CALENDAR_EMPTY_INPUT", "ICS text payload is empty.")

    encoded = ics_text.encode("utf-8")
    if len(encoded) > MAX_ICS_BYTES:
        raise CalendarExtractError(
            "CALENDAR_OVERSIZED_INPUT",
            f"Input exceeds maximum allowed size of {MAX_ICS_BYTES} bytes.",
        )

    for ch in ics_text:
        code = ord(ch)
        if code < 32 and ch not in ("\t", "\n", "\r"):
            raise CalendarExtractError(
                "CALENDAR_CONTROL_CHAR",
                f"Disallowed C0 control character detected: hex({hex(code)})",
            )

    clean_lines = [line.rstrip("\r") for line in ics_text.strip().split("\n")]
    canon_crlf = "\r\n".join(clean_lines) + "\r\n"
    content_sha256 = hashlib.sha256(canon_crlf.encode("utf-8")).hexdigest().lower()

    unfolded = _unfold_lines(ics_text)
    if not unfolded or unfolded[0].strip().upper() != "BEGIN:VCALENDAR" or unfolded[-1].strip().upper() != "END:VCALENDAR":
        raise CalendarExtractError(
            "CALENDAR_INVALID_STRUCTURE",
            "Missing enclosing BEGIN:VCALENDAR / END:VCALENDAR.",
        )

    results: list[dict[str, Any]] = []
    default_tz = "Europe/Vienna"
    in_vevent = False
    in_valarm = False

    current_uid: Optional[str] = None
    current_sequence: int = 0
    current_recurrence_id: Optional[str] = None

    for line in unfolded:
        prop_name, params, value = _parse_prop_line(line)

        if not in_vevent:
            if prop_name == "X-WR-TIMEZONE":
                raw_wr_tz = value.strip().strip('"').strip("'")
                if raw_wr_tz:
                    default_tz = WINDOWS_TZ_ALIASES.get(raw_wr_tz, raw_wr_tz)
            elif prop_name == "BEGIN" and value.strip().upper() == "VEVENT":
                in_vevent = True
                in_valarm = False
                current_uid = None
                current_sequence = 0
                current_recurrence_id = None
            continue

        # Within VEVENT
        if prop_name == "BEGIN" and value.strip().upper() == "VALARM":
            in_valarm = True
            continue
        if prop_name == "END" and value.strip().upper() == "VALARM":
            in_valarm = False
            continue
        if in_valarm:
            # Skip VALARM properties so they never overwrite VEVENT properties
            continue

        if prop_name == "END" and value.strip().upper() == "VEVENT":
            in_vevent = False
            if not current_uid:
                raise CalendarExtractError("CALENDAR_MISSING_UID", "VEVENT missing required UID property.")

            results.append({
                "uid": current_uid,
                "sequence": current_sequence,
                "recurrence_id": current_recurrence_id,
                "content_sha256": content_sha256,
            })
            continue

        # VEVENT properties
        if prop_name == "UID":
            current_uid = value.strip()
        elif prop_name == "SEQUENCE":
            try:
                seq_int = int(value.strip())
                if seq_int >= 0:
                    current_sequence = seq_int
            except ValueError:
                pass
        elif prop_name == "RECURRENCE-ID":
            current_recurrence_id = _parse_recurrence_id(value.strip(), params, default_tz=default_tz)

    if not results:
        raise CalendarExtractError("CALENDAR_INVALID_STRUCTURE", "No VEVENT components found in VCALENDAR.")

    return results
