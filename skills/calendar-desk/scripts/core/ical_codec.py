"""Deterministic RFC-5545 iCalendar Codec and Recurrence Engine.

Provides parsing, line folding/unfolding, text escaping, timezone resolution,
recurrence expansion across DST transitions, and lossless VEVENT updates
using only the Python standard library.
"""

from __future__ import annotations

import calendar
import copy
import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

MAX_ICS_BYTES: int = 1_048_576  # 1 MiB

WINDOWS_TZ_ALIASES: dict[str, str] = {
    "W. Europe Standard Time": "Europe/Vienna",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
    "UTC": "UTC",
}

_WD_MAP: dict[str, int] = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}

_BYDAY_RE = re.compile(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$")


class ICalCodecError(ValueError):
    """Raised when iCalendar data violates RFC 5545 or calendar-desk invariants."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def resolve_tzid(tzid: Optional[str], default_tz: str = "Europe/Vienna") -> ZoneInfo:
    """Resolve an IANA timezone identifier or Windows alias to a ZoneInfo instance."""
    raw = (tzid or "").strip().strip('"').strip("'")
    if not raw:
        raw = default_tz
    target = WINDOWS_TZ_ALIASES.get(raw, raw)
    try:
        return ZoneInfo(target)
    except Exception as exc:
        raise ICalCodecError("ICAL_TIMEZONE_ERROR", f"Unknown timezone identifier: {tzid}") from exc


def unfold_ics_lines(raw_ics: str) -> list[str]:
    """Unfold lines per RFC 5545 §3.1, removing CRLF/LF + single space/tab unconditionally."""
    unfolded_text = re.sub(r"\r?\n[ \t]", "", raw_ics)
    lines: list[str] = []
    for line in unfolded_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.rstrip("\r")
        if stripped:
            lines.append(stripped)
    return lines


def fold_ics_line(line: str, max_octets: int = 75) -> str:
    """Fold a single logical content line to physical lines <= max_octets UTF-8 bytes."""
    raw_bytes = line.encode("utf-8")
    if len(raw_bytes) <= max_octets:
        return line

    chunks: list[str] = []
    current_chars: list[str] = []
    current_octets = 0
    limit = max_octets

    for ch in line:
        ch_bytes_len = len(ch.encode("utf-8"))
        if current_octets + ch_bytes_len > limit:
            chunks.append("".join(current_chars))
            current_chars = [ch]
            current_octets = ch_bytes_len
            limit = max_octets - 1  # 1 octet reserved for continuation space
        else:
            current_chars.append(ch)
            current_octets += ch_bytes_len

    if current_chars:
        chunks.append("".join(current_chars))

    return "\r\n ".join(chunks)


def _format_param_value(val: str) -> str:
    """Format parameter value per RFC 5545 §3.1: quote with DQUOTE if containing :, ;, or ,."""
    if any(ch in val for ch in (":", ";", ",")):
        return f'"{val}"'
    return val


def escape_ics_text(value: str) -> str:
    """Escape text per RFC 5545 §3.3.11."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    out = normalized.replace("\\", "\\\\")
    out = out.replace(";", r"\;")
    out = out.replace(",", r"\,")
    out = out.replace("\n", r"\n")
    return out


def unescape_ics_text(value: str) -> str:
    """Unescape text per RFC 5545 §3.3.11."""
    def _repl(m: re.Match[str]) -> str:
        token = m.group(1)
        if token in ("n", "N"):
            return "\n"
        return token

    return re.sub(r"\\([nN,;\\])", _repl, value)


def _validate_raw_input(raw_ics: str) -> None:
    """Validate input size caps, empty check, and C0 control character hygiene."""
    if not raw_ics or not raw_ics.strip():
        raise ICalCodecError("ICAL_EMPTY_INPUT", "iCalendar input cannot be empty.")
    if len(raw_ics.encode("utf-8")) > MAX_ICS_BYTES:
        raise ICalCodecError(
            "ICAL_OVERSIZED_INPUT",
            f"Input exceeds maximum allowed size of {MAX_ICS_BYTES} bytes.",
        )
    for ch in raw_ics:
        code = ord(ch)
        if code < 32 and ch not in ("\t", "\n", "\r"):
            raise ICalCodecError(
                "ICAL_CONTROL_CHAR",
                f"Disallowed C0 control character detected: hex({hex(code)})",
            )


def _parse_property_line(line: str) -> tuple[str, dict[str, str], str]:
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


def _parse_duration_minutes(dur_str: str) -> int:
    """Parse RFC 5545 DURATION property into minutes."""
    m = re.match(r"^[+-]?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", dur_str)
    if not m:
        return 0
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return days * 1440 + hours * 60 + minutes + (1 if seconds >= 30 else 0)


def _parse_datetime_endpoint(
    dt_str: str,
    params: dict[str, str],
    default_tz: str,
) -> dict[str, Any]:
    """Parse DTSTART / DTEND / RECURRENCE-ID value into endpoint dictionary."""
    is_date = params.get("VALUE", "").upper() == "DATE" or (len(dt_str) == 8 and dt_str.isdigit())

    if is_date:
        try:
            parsed_date = datetime.strptime(dt_str, "%Y%m%d").date()
        except ValueError as exc:
            raise ICalCodecError("ICAL_INVALID_DATETIME", f"Invalid DATE string: {dt_str}") from exc
        return {
            "utc": None,
            "local": None,
            "tzid": None,
            "all_day": True,
            "date": parsed_date.strftime("%Y-%m-%d"),
        }

    # DATE-TIME parsing
    clean_dt = dt_str.strip()
    tz_param = params.get("TZID")
    resolved_tz = resolve_tzid(tz_param, default_tz=default_tz)

    try:
        if clean_dt.endswith("Z"):
            dt_utc = datetime.strptime(clean_dt, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(resolved_tz)
        else:
            naive_dt = datetime.strptime(clean_dt, "%Y%m%dT%H%M%S")
            dt_local = naive_dt.replace(tzinfo=resolved_tz)
            dt_utc = dt_local.astimezone(timezone.utc)
    except ValueError as exc:
        raise ICalCodecError("ICAL_INVALID_DATETIME", f"Invalid DATE-TIME string: {dt_str}") from exc

    return {
        "utc": dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local": dt_local.isoformat(),
        "tzid": resolved_tz.key,
        "all_day": False,
        "date": dt_local.strftime("%Y-%m-%d"),
    }


def parse_ics_to_records(
    raw_ics: str,
    *,
    account: str = "default",
    calendar_alias: str = "default",
    default_tz: str = "Europe/Vienna",
    etag: Optional[str] = None,
    href: Optional[str] = None,
    origin: str = "ics_import",
    raw_ics_relpath: Optional[str] = None,
    ingested_at: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Parse RFC 5545 iCalendar payload into a list of calendar-record/1 dictionaries."""
    _validate_raw_input(raw_ics)

    clean_lines = [line.rstrip("\r") for line in raw_ics.strip().split("\n")]
    canon_crlf = "\r\n".join(clean_lines) + "\r\n"
    content_sha256 = hashlib.sha256(canon_crlf.encode("utf-8")).hexdigest().lower()

    unfolded = unfold_ics_lines(raw_ics)

    if not unfolded or unfolded[0].strip().upper() != "BEGIN:VCALENDAR" or unfolded[-1].strip().upper() != "END:VCALENDAR":
        raise ICalCodecError("ICAL_INVALID_STRUCTURE", "Missing enclosing BEGIN:VCALENDAR / END:VCALENDAR.")

    top_method: Optional[str] = None
    top_wr_tz: Optional[str] = None
    records: list[dict[str, Any]] = []

    in_vevent = False
    in_valarm = False
    current_lines: list[tuple[str, dict[str, str], str]] = []

    for line in unfolded:
        prop_name, params, value = _parse_property_line(line)

        if not in_vevent:
            if prop_name == "METHOD":
                top_method = value.strip().upper()
            elif prop_name == "X-WR-TIMEZONE":
                top_wr_tz = value.strip()
            elif prop_name == "BEGIN" and value.strip().upper() == "VEVENT":
                in_vevent = True
                current_lines = []
            continue

        # In VEVENT
        if prop_name == "BEGIN" and value.strip().upper() == "VALARM":
            in_valarm = True
            continue
        if prop_name == "END" and value.strip().upper() == "VALARM":
            in_valarm = False
            continue
        if in_valarm:
            # Ignore VALARM properties so they never overwrite VEVENT fields
            continue

        if prop_name == "END" and value.strip().upper() == "VEVENT":
            in_vevent = False
            effective_default_tz = top_wr_tz or default_tz
            rec = _build_record_from_lines(
                current_lines,
                account=account,
                calendar_alias=calendar_alias,
                default_tz=effective_default_tz,
                top_method=top_method,
                etag=etag,
                href=href,
                origin=origin,
                raw_ics_relpath=raw_ics_relpath,
                ingested_at=ingested_at,
                content_sha256=content_sha256,
            )
            records.append(rec)
            current_lines = []
            continue

        current_lines.append((prop_name, params, value))

    return records


def _build_record_from_lines(
    lines: list[tuple[str, dict[str, str], str]],
    *,
    account: str,
    calendar_alias: str,
    default_tz: str,
    top_method: Optional[str],
    etag: Optional[str],
    href: Optional[str],
    origin: str,
    raw_ics_relpath: Optional[str],
    ingested_at: Optional[str],
    content_sha256: str,
) -> dict[str, Any]:
    """Construct a calendar-record/1 dict from parsed VEVENT properties."""
    uid: Optional[str] = None
    dtstart_raw: Optional[tuple[str, dict[str, str]]] = None
    dtend_raw: Optional[tuple[str, dict[str, str]]] = None
    duration_str: Optional[str] = None
    recurrence_id_raw: Optional[tuple[str, dict[str, str]]] = None
    sequence = 0
    status = "CONFIRMED"
    transp = "OPAQUE"
    classification = "PUBLIC"
    summary = ""
    description = ""
    location = ""
    rrule: Optional[str] = None
    exdates: list[str] = []
    organizer: Optional[dict[str, str]] = None
    attendees: list[dict[str, Any]] = []

    for prop_name, params, value in lines:
        if prop_name == "UID":
            uid = value.strip()
        elif prop_name == "DTSTART":
            dtstart_raw = (value.strip(), params)
        elif prop_name == "DTEND":
            dtend_raw = (value.strip(), params)
        elif prop_name == "DURATION":
            duration_str = value.strip()
        elif prop_name == "RECURRENCE-ID":
            recurrence_id_raw = (value.strip(), params)
        elif prop_name == "SEQUENCE":
            try:
                sequence = int(value.strip())
            except ValueError:
                sequence = 0
        elif prop_name == "STATUS":
            status = value.strip().upper()
        elif prop_name == "TRANSP":
            transp = value.strip().upper()
        elif prop_name == "CLASS":
            classification = value.strip().upper()
        elif prop_name == "SUMMARY":
            summary = unescape_ics_text(value)
        elif prop_name == "DESCRIPTION":
            description = unescape_ics_text(value)
        elif prop_name == "LOCATION":
            location = unescape_ics_text(value)
        elif prop_name == "RRULE":
            rrule = value.strip()
        elif prop_name == "EXDATE":
            for ex in value.split(","):
                ex_clean = ex.strip()
                if ex_clean:
                    parsed_ex = _parse_datetime_endpoint(ex_clean, params, default_tz)
                    exdates.append(parsed_ex["utc"] or parsed_ex["date"])
        elif prop_name == "ORGANIZER":
            cn = params.get("CN", "").strip().strip('"')
            cal_addr = value.strip()
            email = cal_addr
            if email.lower().startswith("mailto:"):
                email = email[7:]
            organizer = {
                "cn": cn,
                "email": email,
                "cal_address": cal_addr,
            }
        elif prop_name == "ATTENDEE":
            cn = params.get("CN", "").strip().strip('"')
            cal_addr = value.strip()
            email = cal_addr
            if email.lower().startswith("mailto:"):
                email = email[7:]
            partstat = params.get("PARTSTAT", "NEEDS-ACTION").upper()
            role = params.get("ROLE", "REQ-PARTICIPANT").upper()
            rsvp = params.get("RSVP", "FALSE").upper() == "TRUE"
            cutype = params.get("CUTYPE", "INDIVIDUAL").upper()
            attendees.append({
                "cn": cn,
                "email": email,
                "cal_address": cal_addr,
                "partstat": partstat,
                "role": role,
                "rsvp": rsvp,
                "cutype": cutype,
            })

    if not uid:
        raise ICalCodecError("ICAL_MISSING_UID", "VEVENT missing required UID property.")
    if not dtstart_raw:
        raise ICalCodecError("ICAL_MISSING_DTSTART", "VEVENT missing required DTSTART property.")

    start_endpoint = _parse_datetime_endpoint(dtstart_raw[0], dtstart_raw[1], default_tz)

    # Compute DTEND / DURATION
    if dtend_raw:
        end_endpoint = _parse_datetime_endpoint(dtend_raw[0], dtend_raw[1], default_tz)
        if start_endpoint["all_day"]:
            d_start = datetime.strptime(start_endpoint["date"], "%Y-%m-%d").date()
            d_end = datetime.strptime(end_endpoint["date"], "%Y-%m-%d").date()
            duration_minutes = (d_end - d_start).days * 1440
        else:
            dt_s = datetime.fromisoformat(start_endpoint["utc"].replace("Z", "+00:00"))
            dt_e = datetime.fromisoformat(end_endpoint["utc"].replace("Z", "+00:00"))
            duration_minutes = max(0, int((dt_e - dt_s).total_seconds() // 60))
    elif duration_str:
        duration_minutes = _parse_duration_minutes(duration_str)
        if start_endpoint["all_day"]:
            days_span = max(1, duration_minutes // 1440)
            d_start = datetime.strptime(start_endpoint["date"], "%Y-%m-%d").date()
            d_end = d_start + timedelta(days=days_span)
            end_endpoint = {
                "utc": None,
                "local": None,
                "tzid": None,
                "all_day": True,
                "date": d_end.strftime("%Y-%m-%d"),
            }
        else:
            dt_s = datetime.fromisoformat(start_endpoint["utc"].replace("Z", "+00:00"))
            dt_e = dt_s + timedelta(minutes=duration_minutes)
            tz = resolve_tzid(start_endpoint["tzid"], default_tz)
            dt_e_local = dt_e.astimezone(tz)
            end_endpoint = {
                "utc": dt_e.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local": dt_e_local.isoformat(),
                "tzid": tz.key,
                "all_day": False,
                "date": dt_e_local.strftime("%Y-%m-%d"),
            }
    else:
        # Default end
        if start_endpoint["all_day"]:
            d_start = datetime.strptime(start_endpoint["date"], "%Y-%m-%d").date()
            d_end = d_start + timedelta(days=1)
            duration_minutes = 1440
            end_endpoint = {
                "utc": None,
                "local": None,
                "tzid": None,
                "all_day": True,
                "date": d_end.strftime("%Y-%m-%d"),
            }
        else:
            duration_minutes = 0
            end_endpoint = dict(start_endpoint)

    recurrence_id: Optional[str] = None
    if recurrence_id_raw:
        parsed_rec = _parse_datetime_endpoint(recurrence_id_raw[0], recurrence_id_raw[1], default_tz)
        recurrence_id = parsed_rec["utc"] or parsed_rec["date"]

    return {
        "schema": "calendar-record/1",
        "account": account,
        "calendar_alias": calendar_alias,
        "uid": uid,
        "recurrence_id": recurrence_id,
        "sequence": sequence,
        "etag": etag,
        "href": href,
        "method": top_method,
        "status": status,
        "transp": transp,
        "summary": summary,
        "description": description,
        "location": location,
        "start": start_endpoint,
        "end": end_endpoint,
        "duration_minutes": duration_minutes,
        "rrule": rrule,
        "exdates": exdates,
        "organizer": organizer,
        "attendees": attendees,
        "classification": classification,
        "source_provenance": {
            "origin": origin,
            "raw_ics_relpath": raw_ics_relpath,
            "ingested_at": ingested_at,
        },
        "content_sha256": content_sha256,
    }


def extract_calendar_object_ref(record_or_ics: dict[str, Any] | str) -> dict[str, Any]:
    """Extract standard calendar_objects handoff item for mail-desk and event-documentation."""
    if isinstance(record_or_ics, str):
        records = parse_ics_to_records(record_or_ics)
        if not records:
            raise ICalCodecError("ICAL_INVALID_STRUCTURE", "No VEVENT found in ICS string.")
        record = records[0]
    else:
        record = record_or_ics

    return {
        "uid": str(record.get("uid", "")),
        "sequence": int(record.get("sequence", 0)),
        "recurrence_id": record.get("recurrence_id"),
        "content_sha256": str(record.get("content_sha256", "")),
    }


def _nth_weekday_of_month(year: int, month: int, weekday_num: int, n: int) -> Optional[int]:
    """Calculate the day of the month for the nth weekday (e.g. 1st MO, -1st FR)."""
    cal = calendar.monthcalendar(year, month)
    days = [week[weekday_num] for week in cal if week[weekday_num] != 0]
    if not days:
        return None
    if n > 0:
        return days[n - 1] if n <= len(days) else None
    else:
        idx = len(days) + n
        return days[idx] if 0 <= idx < len(days) else None


def expand_record_occurrences(
    record: dict[str, Any],
    window_start_utc: str,
    window_end_utc: str,
    *,
    override_records: Sequence[dict[str, Any]] = (),
    max_instances: int = 500,
) -> list[dict[str, Any]]:
    """Deterministically expand recurring events across DST and apply exception overrides."""
    win_start_dt = datetime.fromisoformat(window_start_utc.replace("Z", "+00:00"))
    if win_start_dt.tzinfo is None:
        win_start_dt = win_start_dt.replace(tzinfo=timezone.utc)
    win_end_dt = datetime.fromisoformat(window_end_utc.replace("Z", "+00:00"))
    if win_end_dt.tzinfo is None:
        win_end_dt = win_end_dt.replace(tzinfo=timezone.utc)

    def _overlaps(st_dt: datetime, end_dt: datetime) -> bool:
        return end_dt > win_start_dt and st_dt < win_end_dt

    is_all_day = bool(record["start"].get("all_day"))

    # Non-recurring event
    if not record.get("rrule"):
        if is_all_day:
            st_dt = datetime.fromisoformat(record["start"]["date"] + "T00:00:00+00:00")
            end_dt = datetime.fromisoformat(record["end"]["date"] + "T00:00:00+00:00")
        else:
            st_dt = datetime.fromisoformat(record["start"]["utc"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(record["end"]["utc"].replace("Z", "+00:00"))
        return [record] if _overlaps(st_dt, end_dt) else []

    # Parse and validate RRULE
    rrule_str = record["rrule"]
    rrule_parts: dict[str, str] = {}
    for part in rrule_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rrule_parts[k.strip().upper()] = v.strip().upper()

    supported_rrule_keys = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}
    for k in rrule_parts:
        if k not in supported_rrule_keys:
            raise ICalCodecError("ICAL_UNSUPPORTED_RRULE", f"Unhandled RRULE rule part: {k}")

    freq = rrule_parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise ICalCodecError("ICAL_INVALID_RRULE", f"Unsupported or missing FREQ in RRULE: {rrule_str}")

    if "INTERVAL" in rrule_parts:
        try:
            interval = int(rrule_parts["INTERVAL"])
            if interval < 1:
                raise ValueError()
        except ValueError:
            raise ICalCodecError("ICAL_INVALID_RRULE", f"Invalid INTERVAL in RRULE: {rrule_parts['INTERVAL']}")
    else:
        interval = 1

    if "COUNT" in rrule_parts:
        try:
            count = int(rrule_parts["COUNT"])
            if count < 0:
                raise ValueError()
        except ValueError:
            raise ICalCodecError("ICAL_INVALID_RRULE", f"Invalid COUNT in RRULE: {rrule_parts['COUNT']}")
    else:
        count = None

    until_dt: Optional[datetime] = None
    if "UNTIL" in rrule_parts:
        until_val = rrule_parts["UNTIL"]
        try:
            if until_val.endswith("Z"):
                until_dt = datetime.strptime(until_val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            elif len(until_val) == 8 and until_val.isdigit():
                until_dt = datetime.strptime(until_val, "%Y%m%d").replace(tzinfo=timezone.utc)
            else:
                until_dt = datetime.strptime(until_val, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ICalCodecError("ICAL_INVALID_RRULE", f"Invalid UNTIL date in RRULE: {until_val}") from exc

    byday_str = rrule_parts.get("BYDAY", "")
    bydays = [b.strip() for b in byday_str.split(",") if b.strip()]
    if bydays:
        for b in bydays:
            m = _BYDAY_RE.match(b)
            if not m:
                raise ICalCodecError("ICAL_INVALID_RRULE", f"Invalid BYDAY code: {b}")
            ordinal_str = m.group(1)
            if ordinal_str is not None:
                ordinal = int(ordinal_str)
                if ordinal == 0:
                    raise ICalCodecError("ICAL_INVALID_RRULE", "BYDAY ordinal cannot be zero.")
                if freq in ("WEEKLY", "DAILY"):
                    raise ICalCodecError("ICAL_INVALID_RRULE", f"BYDAY ordinal not allowed with FREQ={freq}: {b}")
                elif freq == "MONTHLY":
                    if ordinal < -5 or ordinal > 5:
                        raise ICalCodecError("ICAL_INVALID_RRULE", f"Monthly BYDAY ordinal out of range (-5..5): {ordinal}")
                elif freq == "YEARLY":
                    if ordinal < -53 or ordinal > 53:
                        raise ICalCodecError("ICAL_INVALID_RRULE", f"Yearly BYDAY ordinal out of range (-53..53): {ordinal}")

    exdates = set(record.get("exdates", []))
    override_by_id: dict[str, dict[str, Any]] = {}
    for ovr in override_records:
        r_id = ovr.get("recurrence_id")
        if r_id:
            override_by_id[r_id] = ovr

    dur_minutes = record.get("duration_minutes", 0)

    if is_all_day:
        start_local_date = datetime.strptime(record["start"]["date"], "%Y-%m-%d").date()
        tz = None
        start_time = time(0, 0, 0)
    else:
        tz_name = record["start"].get("tzid") or "Europe/Vienna"
        tz = resolve_tzid(tz_name)
        start_local = datetime.fromisoformat(record["start"]["local"])
        start_local_date = start_local.date()
        start_time = time(start_local.hour, start_local.minute, start_local.second)

    stop_horizon_utc = win_end_dt
    for ovr in override_records:
        r_id = ovr.get("recurrence_id")
        if r_id:
            try:
                if "T" in r_id:
                    dt_ovr = datetime.fromisoformat(r_id.replace("Z", "+00:00"))
                    if dt_ovr.tzinfo is None:
                        dt_ovr = dt_ovr.replace(tzinfo=tz or timezone.utc).astimezone(timezone.utc)
                    else:
                        dt_ovr = dt_ovr.astimezone(timezone.utc)
                else:
                    dt_ovr = datetime.strptime(r_id, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if dt_ovr > stop_horizon_utc:
                    stop_horizon_utc = dt_ovr
            except Exception:
                pass

    occurrences: list[dict[str, Any]] = []
    generated_count = 0

    def _process_candidate(cand_date: date) -> bool:
        """Evaluate a candidate recurrence date against UNTIL, COUNT, EXDATE, override, and window.

        Returns True if the generation loop should terminate.
        """
        nonlocal generated_count
        if cand_date < start_local_date:
            return False

        if is_all_day:
            cand_start_utc = datetime(cand_date.year, cand_date.month, cand_date.day, tzinfo=timezone.utc)
            cand_end_utc = cand_start_utc + timedelta(minutes=dur_minutes)
            cand_local = None
            utc_key = cand_date.strftime("%Y-%m-%d")
            date_key = utc_key
        else:
            cand_local = datetime.combine(cand_date, start_time, tzinfo=tz)
            cand_start_utc = cand_local.astimezone(timezone.utc)
            cand_end_local = cand_local + timedelta(minutes=dur_minutes)
            cand_end_utc = cand_end_local.astimezone(timezone.utc)
            utc_key = cand_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            date_key = cand_date.strftime("%Y-%m-%d")

        if until_dt and cand_start_utc > until_dt:
            return True

        generated_count += 1
        if count is not None and generated_count > count:
            return True

        if utc_key in exdates or date_key in exdates:
            return cand_start_utc >= stop_horizon_utc

        if utc_key in override_by_id or date_key in override_by_id:
            ovr = override_by_id.get(utc_key) or override_by_id[date_key]
            if ovr.get("status") == "CANCELLED":
                return cand_start_utc >= stop_horizon_utc
            if ovr["start"].get("all_day"):
                st_ovr = datetime.fromisoformat(ovr["start"]["date"] + "T00:00:00+00:00")
                end_ovr = datetime.fromisoformat(ovr["end"]["date"] + "T00:00:00+00:00")
            else:
                st_ovr = datetime.fromisoformat(ovr["start"]["utc"].replace("Z", "+00:00"))
                end_ovr = datetime.fromisoformat(ovr["end"]["utc"].replace("Z", "+00:00"))
            if _overlaps(st_ovr, end_ovr):
                occurrences.append(ovr)
            return cand_start_utc >= stop_horizon_utc

        if _overlaps(cand_start_utc, cand_end_utc):
            occ = copy.deepcopy(record)
            if is_all_day:
                occ["start"]["date"] = cand_date.strftime("%Y-%m-%d")
                end_date = cand_date + timedelta(days=max(1, dur_minutes // 1440))
                occ["end"]["date"] = end_date.strftime("%Y-%m-%d")
            else:
                occ["start"]["utc"] = cand_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                occ["start"]["local"] = cand_local.isoformat()
                occ["start"]["date"] = cand_local.strftime("%Y-%m-%d")
                occ["end"]["utc"] = cand_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                occ["end"]["local"] = cand_end_local.isoformat()
                occ["end"]["date"] = cand_end_local.strftime("%Y-%m-%d")
            occurrences.append(occ)

        if cand_start_utc >= stop_horizon_utc:
            return True

        return False

    loop_iterations = 0
    if freq == "DAILY":
        target_wd_nums = set(_WD_MAP[b[-2:]] for b in bydays) if bydays else None
        curr_date = start_local_date
        while True:
            loop_iterations += 1
            if loop_iterations > 10000:
                raise ICalCodecError("ICAL_INVALID_RRULE", "Infinite loop detected: recurrence expansion timed out")
            if count is not None and generated_count >= count:
                break
            if len(occurrences) >= max_instances:
                break
            if target_wd_nums is None or curr_date.weekday() in target_wd_nums:
                if _process_candidate(curr_date):
                    break
            else:
                cand_dt = (
                    datetime(curr_date.year, curr_date.month, curr_date.day, tzinfo=timezone.utc)
                    if is_all_day
                    else datetime.combine(curr_date, start_time, tzinfo=tz).astimezone(timezone.utc)
                )
                if cand_dt >= stop_horizon_utc:
                    break
            curr_date += timedelta(days=interval)

    elif freq == "WEEKLY":
        target_wd_nums = [_WD_MAP[b[-2:]] for b in bydays] if bydays else [start_local_date.weekday()]
        current_week_monday = start_local_date - timedelta(days=start_local_date.weekday())
        while True:
            loop_iterations += 1
            if loop_iterations > 10000:
                raise ICalCodecError("ICAL_INVALID_RRULE", "Infinite loop detected: recurrence expansion timed out")
            if count is not None and generated_count >= count:
                break
            if len(occurrences) >= max_instances:
                break
            stop = False
            for wd in sorted(target_wd_nums):
                c_date = current_week_monday + timedelta(days=wd)
                if c_date < start_local_date:
                    continue
                if _process_candidate(c_date):
                    stop = True
                    break
            if stop:
                break
            current_week_monday += timedelta(weeks=interval)

    elif freq == "MONTHLY":
        curr_year = start_local_date.year
        curr_month = start_local_date.month
        while True:
            loop_iterations += 1
            if loop_iterations > 10000:
                raise ICalCodecError("ICAL_INVALID_RRULE", "Infinite loop detected: recurrence expansion timed out")
            if count is not None and generated_count >= count:
                break
            if len(occurrences) >= max_instances:
                break
            first_of_month_utc = datetime(curr_year, curr_month, 1, tzinfo=timezone.utc)
            if first_of_month_utc >= stop_horizon_utc:
                break
            if until_dt and first_of_month_utc > until_dt:
                break
            stop = False
            if bydays:
                month_dates: list[date] = []
                for bday in bydays:
                    code = bday[-2:]
                    wd_num = _WD_MAP[code]
                    prefix_str = bday[:-2]
                    n = int(prefix_str) if prefix_str else 1
                    day_num = _nth_weekday_of_month(curr_year, curr_month, wd_num, n)
                    if day_num is not None:
                        month_dates.append(date(curr_year, curr_month, day_num))
                for c_date in sorted(set(month_dates)):
                    if _process_candidate(c_date):
                        stop = True
                        break
            else:
                max_day = calendar.monthrange(curr_year, curr_month)[1]
                if start_local_date.day <= max_day:
                    c_date = date(curr_year, curr_month, start_local_date.day)
                    if _process_candidate(c_date):
                        stop = True
            if stop:
                break
            curr_month += interval
            while curr_month > 12:
                curr_month -= 12
                curr_year += 1

    elif freq == "YEARLY":
        curr_year = start_local_date.year
        while True:
            loop_iterations += 1
            if loop_iterations > 10000:
                raise ICalCodecError("ICAL_INVALID_RRULE", "Infinite loop detected: recurrence expansion timed out")
            if count is not None and generated_count >= count:
                break
            if len(occurrences) >= max_instances:
                break
            first_of_year_utc = datetime(curr_year, 1, 1, tzinfo=timezone.utc)
            if first_of_year_utc >= stop_horizon_utc:
                break
            if until_dt and first_of_year_utc > until_dt:
                break
            stop = False
            if bydays:
                year_dates: list[date] = []
                for bday in bydays:
                    code = bday[-2:]
                    wd_num = _WD_MAP[code]
                    prefix_str = bday[:-2]
                    n = int(prefix_str) if prefix_str else 1
                    day_num = _nth_weekday_of_month(curr_year, start_local_date.month, wd_num, n)
                    if day_num is not None:
                        year_dates.append(date(curr_year, start_local_date.month, day_num))
                for c_date in sorted(set(year_dates)):
                    if _process_candidate(c_date):
                        stop = True
                        break
            else:
                max_day = calendar.monthrange(curr_year, start_local_date.month)[1]
                if start_local_date.day <= max_day:
                    c_date = date(curr_year, start_local_date.month, start_local_date.day)
                    if _process_candidate(c_date):
                        stop = True
            if stop:
                break
            curr_year += interval

    def _sort_key(occ: dict[str, Any]) -> str:
        return occ["start"]["utc"] or occ["start"]["date"]

    occurrences.sort(key=_sort_key)
    return occurrences[:max_instances]


def build_vevent_ics(record: Mapping[str, Any]) -> str:
    """Serialize a calendar-record/1 dictionary into a standards-compliant VCALENDAR payload."""
    guard_fields = ("uid", "status", "transp", "classification", "rrule", "method", "recurrence_id")
    for f in guard_fields:
        val = record.get(f)
        if val is not None and isinstance(val, str):
            if "\r" in val or "\n" in val:
                raise ICalCodecError("ICAL_INJECTION_DETECTED", f"Newline in property value: {f}")
    seq = record.get("sequence")
    if seq is not None and isinstance(seq, str) and ("\r" in seq or "\n" in seq):
        raise ICalCodecError("ICAL_INJECTION_DETECTED", "Newline in property value: sequence")

    start = record["start"]
    end = record["end"]

    # Validate end >= start (end cannot precede start)
    if start.get("all_day") or end.get("all_day"):
        st_date_str = start.get("date")
        end_date_str = end.get("date")
        if st_date_str and end_date_str:
            try:
                st_d = datetime.strptime(st_date_str, "%Y-%m-%d").date()
                end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                if end_d < st_d:
                    raise ICalCodecError("ICAL_INVALID_DATETIME", "DTEND cannot precede DTSTART.")
            except ValueError as exc:
                raise ICalCodecError("ICAL_INVALID_DATETIME", f"Invalid date: {exc}") from exc
    else:
        st_utc_str = start.get("utc")
        end_utc_str = end.get("utc")
        if st_utc_str and end_utc_str:
            try:
                st_dt = datetime.fromisoformat(st_utc_str.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_utc_str.replace("Z", "+00:00"))
                if end_dt < st_dt:
                    raise ICalCodecError("ICAL_INVALID_DATETIME", "DTEND cannot precede DTSTART.")
            except ValueError as exc:
                raise ICalCodecError("ICAL_INVALID_DATETIME", f"Invalid datetime: {exc}") from exc
        elif start.get("local") and end.get("local"):
            try:
                st_dt = datetime.fromisoformat(start["local"])
                end_dt = datetime.fromisoformat(end["local"])
                if end_dt < st_dt:
                    raise ICalCodecError("ICAL_INVALID_DATETIME", "DTEND cannot precede DTSTART.")
            except ValueError as exc:
                raise ICalCodecError("ICAL_INVALID_DATETIME", f"Invalid datetime: {exc}") from exc

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//office-intelligence//calendar-desk//EN",
        "CALSCALE:GREGORIAN",
    ]
    if record.get("method"):
        lines.append(f"METHOD:{record['method']}")

    lines.append("BEGIN:VEVENT")
    lines.append(f"UID:{record['uid']}")

    dtstamp_val = record.get("dtstamp")
    if dtstamp_val:
        dtstamp_clean = dtstamp_val.replace("-", "").replace(":", "")
        if not dtstamp_clean.endswith("Z"):
            dtstamp_clean += "Z"
        lines.append(f"DTSTAMP:{dtstamp_clean}")
    else:
        lines.append(f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")

    lines.append(f"SEQUENCE:{record.get('sequence', 0)}")
    lines.append(f"STATUS:{record.get('status', 'CONFIRMED')}")
    lines.append(f"TRANSP:{record.get('transp', 'OPAQUE')}")
    lines.append(f"CLASS:{record.get('classification', 'PUBLIC')}")

    if record.get("summary"):
        lines.append(f"SUMMARY:{escape_ics_text(record['summary'])}")
    if record.get("description"):
        lines.append(f"DESCRIPTION:{escape_ics_text(record['description'])}")
    if record.get("location"):
        lines.append(f"LOCATION:{escape_ics_text(record['location'])}")

    if start.get("all_day"):
        st_date = start["date"].replace("-", "")
        end_date = end["date"].replace("-", "")
        lines.append(f"DTSTART;VALUE=DATE:{st_date}")
        lines.append(f"DTEND;VALUE=DATE:{end_date}")
    else:
        tz = start.get("tzid")
        if tz and tz != "UTC":
            st_clean = start["local"].replace("-", "").replace(":", "")[:15]
            end_clean = end["local"].replace("-", "").replace(":", "")[:15]
            lines.append(f"DTSTART;TZID={tz}:{st_clean}")
            lines.append(f"DTEND;TZID={tz}:{end_clean}")
        else:
            st_utc = start["utc"].replace("-", "").replace(":", "")
            end_utc = end["utc"].replace("-", "").replace(":", "")
            lines.append(f"DTSTART:{st_utc}")
            lines.append(f"DTEND:{end_utc}")

    if record.get("rrule"):
        lines.append(f"RRULE:{record['rrule']}")

    org = record.get("organizer")
    if isinstance(org, dict):
        for k in ("cn", "cal_address"):
            v = org.get(k)
            if v and isinstance(v, str) and ("\r" in v or "\n" in v):
                raise ICalCodecError("ICAL_INJECTION_DETECTED", f"Newline in property value: organizer.{k}")
        cn = org.get("cn", "")
        cal_addr = org.get("cal_address", "")
        if cn:
            lines.append(f"ORGANIZER;CN={_format_param_value(cn)}:{cal_addr}")
        else:
            lines.append(f"ORGANIZER:{cal_addr}")

    attendees = record.get("attendees", [])
    for att in attendees:
        if isinstance(att, dict):
            for k in ("cn", "cal_address", "cutype", "role", "partstat"):
                v = att.get(k)
                if v and isinstance(v, str) and ("\r" in v or "\n" in v):
                    raise ICalCodecError("ICAL_INJECTION_DETECTED", f"Newline in property value: attendee.{k}")
        params_list: list[str] = []
        if att.get("cutype"):
            params_list.append(f"CUTYPE={_format_param_value(att['cutype'])}")
        if att.get("role"):
            params_list.append(f"ROLE={_format_param_value(att['role'])}")
        if att.get("partstat"):
            params_list.append(f"PARTSTAT={_format_param_value(att['partstat'])}")
        if "rsvp" in att:
            params_list.append(f"RSVP={'TRUE' if att['rsvp'] else 'FALSE'}")
        if att.get("cn"):
            params_list.append(f"CN={_format_param_value(att['cn'])}")
        param_str = ";" + ";".join(params_list) if params_list else ""
        lines.append(f"ATTENDEE{param_str}:{att.get('cal_address', '')}")

    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")

    folded_lines = [fold_ics_line(l) for l in lines]
    return "\r\n".join(folded_lines) + "\r\n"


def _format_endpoint_line(prop: str, val: Any) -> str:
    """Format a start or end value as an RFC-5545 property line."""
    if isinstance(val, dict):
        if val.get("all_day"):
            d_str = val["date"].replace("-", "")
            return f"{prop};VALUE=DATE:{d_str}"
        tz = val.get("tzid")
        if tz and tz != "UTC":
            local_str = val["local"].replace("-", "").replace(":", "")[:15]
            return f"{prop};TZID={tz}:{local_str}"
        utc_str = val["utc"].replace("-", "").replace(":", "")
        return f"{prop}:{utc_str}"
    val_str = str(val).strip()
    if val_str.upper().startswith(f"{prop}:") or val_str.upper().startswith(f"{prop};"):
        return val_str
    return f"{prop}:{val_str}"


def update_vevent_ics(
    raw_ics: str,
    updates: Mapping[str, Any],
    *,
    recurrence_id: Optional[str] = None,
    increment_sequence: bool = True,
) -> str:
    """Update primary VEVENT in raw_ics, incrementing SEQUENCE and preserving X-* and VALARM."""
    unfolded = unfold_ics_lines(raw_ics)

    blocks: list[dict[str, Any]] = []
    current_vevent_lines: Optional[list[str]] = None
    current_raw_lines: list[str] = []

    for line in unfolded:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "BEGIN:VEVENT" and current_vevent_lines is None:
            if current_raw_lines:
                blocks.append({"type": "raw", "lines": current_raw_lines})
                current_raw_lines = []
            current_vevent_lines = [line]
        elif stripped == "END:VEVENT" and current_vevent_lines is not None:
            current_vevent_lines.append(line)
            blocks.append({"type": "vevent", "lines": current_vevent_lines})
            current_vevent_lines = None
        else:
            if current_vevent_lines is not None:
                current_vevent_lines.append(line)
            else:
                current_raw_lines.append(line)

    if current_raw_lines:
        blocks.append({"type": "raw", "lines": current_raw_lines})

    vevent_blocks = [b for b in blocks if b["type"] == "vevent"]
    for block in vevent_blocks:
        rec_id_val = None
        rec_id_params = {}
        for l in block["lines"]:
            p_name, p_params, p_val = _parse_property_line(l)
            if p_name == "RECURRENCE-ID":
                rec_id_val = p_val.strip()
                rec_id_params = p_params
                break
        block["rec_id_raw"] = rec_id_val
        block["rec_id_params"] = rec_id_params
        block["is_master"] = (rec_id_val is None)

    target_block = None
    if recurrence_id is None:
        for b in vevent_blocks:
            if b["is_master"]:
                target_block = b
                break
        if target_block is None and vevent_blocks:
            target_block = vevent_blocks[0]
    else:
        target_norm = recurrence_id.strip()
        target_clean = target_norm.replace("-", "").replace(":", "").replace("Z", "")
        for b in vevent_blocks:
            raw_v = b["rec_id_raw"]
            if not raw_v:
                continue
            raw_clean = raw_v.replace("-", "").replace(":", "").replace("Z", "")
            if target_clean == raw_clean:
                target_block = b
                break
            try:
                ep = _parse_datetime_endpoint(raw_v, b["rec_id_params"], default_tz="Europe/Vienna")
                if target_norm in (ep.get("utc"), ep.get("local"), ep.get("date")):
                    target_block = b
                    break
            except Exception:
                pass
        if target_block is None and vevent_blocks:
            target_block = vevent_blocks[0]

    utc_now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_lines: list[str] = []

    for block in blocks:
        if block is not target_block:
            out_lines.extend(block["lines"])
            continue

        in_valarm = False
        seq_updated = False
        last_mod_updated = False
        handled_keys: set[str] = set()

        for line in block["lines"]:
            stripped = line.strip()
            if stripped == "BEGIN:VEVENT":
                out_lines.append(line)
                continue
            if stripped == "END:VEVENT":
                if "location" in updates and "LOCATION" not in handled_keys:
                    out_lines.append(f"LOCATION:{escape_ics_text(str(updates['location']))}")
                if "summary" in updates and "SUMMARY" not in handled_keys:
                    out_lines.append(f"SUMMARY:{escape_ics_text(str(updates['summary']))}")
                if "description" in updates and "DESCRIPTION" not in handled_keys:
                    out_lines.append(f"DESCRIPTION:{escape_ics_text(str(updates['description']))}")
                if "status" in updates and "STATUS" not in handled_keys:
                    out_lines.append(f"STATUS:{str(updates['status']).upper()}")
                if "transp" in updates and "TRANSP" not in handled_keys:
                    out_lines.append(f"TRANSP:{str(updates['transp']).upper()}")
                if "classification" in updates and "CLASS" not in handled_keys:
                    out_lines.append(f"CLASS:{str(updates['classification']).upper()}")
                if "start" in updates and "START" not in handled_keys:
                    out_lines.append(_format_endpoint_line("DTSTART", updates["start"]))
                if "end" in updates and "END" not in handled_keys:
                    out_lines.append(_format_endpoint_line("DTEND", updates["end"]))
                if "rrule" in updates and "RRULE" not in handled_keys:
                    out_lines.append(f"RRULE:{updates['rrule']}")
                if increment_sequence and not seq_updated:
                    out_lines.append("SEQUENCE:1")
                if not last_mod_updated:
                    out_lines.append(f"LAST-MODIFIED:{utc_now}")
                out_lines.append(line)
                continue

            if stripped == "BEGIN:VALARM":
                in_valarm = True
                out_lines.append(line)
                continue
            if stripped == "END:VALARM":
                in_valarm = False
                out_lines.append(line)
                continue
            if in_valarm:
                out_lines.append(line)
                continue

            prop_name = line.split(":", 1)[0].split(";", 1)[0].upper().strip()
            if prop_name == "SEQUENCE":
                if increment_sequence:
                    try:
                        old_seq = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        old_seq = 0
                    out_lines.append(f"SEQUENCE:{old_seq + 1}")
                    seq_updated = True
                else:
                    out_lines.append(line)
                continue
            if prop_name == "LAST-MODIFIED":
                out_lines.append(f"LAST-MODIFIED:{utc_now}")
                last_mod_updated = True
                continue
            if prop_name == "SUMMARY" and "summary" in updates:
                out_lines.append(f"SUMMARY:{escape_ics_text(str(updates['summary']))}")
                handled_keys.add("SUMMARY")
                continue
            if prop_name == "DESCRIPTION" and "description" in updates:
                out_lines.append(f"DESCRIPTION:{escape_ics_text(str(updates['description']))}")
                handled_keys.add("DESCRIPTION")
                continue
            if prop_name == "LOCATION" and "location" in updates:
                out_lines.append(f"LOCATION:{escape_ics_text(str(updates['location']))}")
                handled_keys.add("LOCATION")
                continue
            if prop_name == "STATUS" and "status" in updates:
                out_lines.append(f"STATUS:{str(updates['status']).upper()}")
                handled_keys.add("STATUS")
                continue
            if prop_name == "TRANSP" and "transp" in updates:
                out_lines.append(f"TRANSP:{str(updates['transp']).upper()}")
                handled_keys.add("TRANSP")
                continue
            if prop_name == "CLASS" and "classification" in updates:
                out_lines.append(f"CLASS:{str(updates['classification']).upper()}")
                handled_keys.add("CLASS")
                continue
            if prop_name == "DTSTART" and "start" in updates:
                out_lines.append(_format_endpoint_line("DTSTART", updates["start"]))
                handled_keys.add("START")
                continue
            if prop_name == "DTEND" and "end" in updates:
                out_lines.append(_format_endpoint_line("DTEND", updates["end"]))
                handled_keys.add("END")
                continue
            if prop_name == "RRULE" and "rrule" in updates:
                out_lines.append(f"RRULE:{updates['rrule']}")
                handled_keys.add("RRULE")
                continue

            out_lines.append(line)

    folded_lines = [fold_ics_line(l) for l in out_lines]
    return "\r\n".join(folded_lines) + "\r\n"
