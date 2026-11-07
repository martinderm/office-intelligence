"""Unit tests for RFC-5545 iCalendar Codec (skills/calendar-desk/scripts/core/ical_codec.py).

Pins deterministic RFC 5545 parsing, line folding/unfolding, text escaping,
timezone alias resolution, recurrence expansion across DST, lossless VEVENT
updating, and fail-closed error handling without any external dependencies.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import sys
import threading
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

# Add calendar-desk scripts to sys.path so core package is importable
CALENDAR_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALENDAR_DESK_ROOT / "scripts"))

from core.ical_codec import (  # noqa: E402
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

FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "calendar-record-v1.json"


class TestICalCodecError(unittest.TestCase):
    """Test custom ICalCodecError exception and error code contracts."""

    def test_exception_inheritance_and_code(self) -> None:
        err = ICalCodecError("ICAL_EMPTY_INPUT", "ICS input cannot be empty")
        self.assertIsInstance(err, ValueError)
        self.assertEqual(err.code, "ICAL_EMPTY_INPUT")
        self.assertIn("ICS input cannot be empty", str(err))

    def test_standard_error_codes(self) -> None:
        expected_codes = {
            "ICAL_EMPTY_INPUT",
            "ICAL_OVERSIZED_INPUT",
            "ICAL_CONTROL_CHAR",
            "ICAL_INVALID_STRUCTURE",
            "ICAL_MISSING_UID",
            "ICAL_MISSING_DTSTART",
            "ICAL_INVALID_DATETIME",
            "ICAL_INVALID_RRULE",
            "ICAL_TIMEZONE_ERROR",
        }
        for code in expected_codes:
            err = ICalCodecError(code, f"Testing code {code}")
            self.assertEqual(err.code, code)


class TestInputBoundsAndHygiene(unittest.TestCase):
    """Test size caps, empty input rejection, and C0 control character guards."""

    def test_empty_input_rejected(self) -> None:
        for empty_val in ("", "   ", "\r\n\t\n"):
            with self.assertRaises(ICalCodecError) as ctx:
                parse_ics_to_records(empty_val)
            self.assertEqual(ctx.exception.code, "ICAL_EMPTY_INPUT")

    def test_oversized_input_rejected(self) -> None:
        # > 1 MiB (1_048_576 bytes)
        huge_ics = "BEGIN:VCALENDAR\r\n" + ("X-FILLER:" + "A" * 1000 + "\r\n") * 1050 + "END:VCALENDAR\r\n"
        self.assertGreater(len(huge_ics.encode("utf-8")), 1_048_576)
        with self.assertRaises(ICalCodecError) as ctx:
            parse_ics_to_records(huge_ics)
        self.assertEqual(ctx.exception.code, "ICAL_OVERSIZED_INPUT")

    def test_control_character_rejected(self) -> None:
        # NUL byte
        nul_ics = "BEGIN:VCALENDAR\r\nUID:1\x00\r\nEND:VCALENDAR"
        with self.assertRaises(ICalCodecError) as ctx:
            parse_ics_to_records(nul_ics)
        self.assertEqual(ctx.exception.code, "ICAL_CONTROL_CHAR")

        # C0 control char e.g. \x07 (BEL) or \x1b (ESC)
        ctrl_ics = "BEGIN:VCALENDAR\r\nSUMMARY:Meeting\x07Alert\r\nEND:VCALENDAR"
        with self.assertRaises(ICalCodecError) as ctx:
            parse_ics_to_records(ctrl_ics)
        self.assertEqual(ctx.exception.code, "ICAL_CONTROL_CHAR")

    def test_allowed_whitespace_permitted(self) -> None:
        valid_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:white-space-001\r\n"
            "DTSTART:20261110T090000Z\r\n"
            "SUMMARY:Tab\tand\tSpace\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        records = parse_ics_to_records(valid_ics)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["summary"], "Tab\tand\tSpace")


class TestLineUnfoldingAndFolding(unittest.TestCase):
    """Test RFC 5545 §3.1 line folding and unfolding rules."""

    def test_unfold_space_and_tab(self) -> None:
        raw = "DESCRIPTION:This is a very long line that was folded \r\n across multiple physical \r\n\tlines with spaces and tabs."
        unfolded = unfold_ics_lines(raw)
        self.assertEqual(len(unfolded), 1)
        self.assertEqual(
            unfolded[0],
            "DESCRIPTION:This is a very long line that was folded across multiple physical lines with spaces and tabs.",
        )

    def test_unfold_mixed_crlf_and_lf(self) -> None:
        raw = "SUMMARY:Line One\nSUMMARY:Line Two folded \n with space\r\nSUMMARY:Line Three"
        unfolded = unfold_ics_lines(raw)
        self.assertEqual(len(unfolded), 3)
        self.assertEqual(unfolded[0], "SUMMARY:Line One")
        self.assertEqual(unfolded[1], "SUMMARY:Line Two folded with space")
        self.assertEqual(unfolded[2], "SUMMARY:Line Three")

    def test_unfold_ascii_midword_fold_roundtrip(self) -> None:
        # Verifies that folding mid-word on an ASCII string does not have spaces injected on unfold
        long_ascii = "SUMMARY:" + "a" * 70
        folded = fold_ics_line(long_ascii, max_octets=75)
        self.assertIn("\r\n ", folded)
        unfolded = unfold_ics_lines(folded)
        self.assertEqual(unfolded, [long_ascii])

    def test_fold_short_line_unchanged(self) -> None:
        line = "SUMMARY:Short subject"
        folded = fold_ics_line(line, max_octets=75)
        self.assertEqual(folded, line)

    def test_fold_multibyte_utf8_preserves_codepoints(self) -> None:
        # Construct line where 75-octet boundary falls right inside a 4-octet emoji or 2-octet umlaut
        prefix = "SUMMARY:" + "x" * 65  # 73 octets
        # Next char: 'ä' is 2 octets, total 75 octets.
        # Following char: '🚀' is 4 octets. It must not be split across fold boundary.
        line = prefix + "ä" + "🚀" + "ö" * 20
        folded = fold_ics_line(line, max_octets=75)
        parts = folded.split("\r\n ")
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-8")), 75)
            # Ensure every part is valid UTF-8 and does not have replacement characters
            part.encode("utf-8").decode("utf-8")

        # Roundtrip unfolding
        unfolded = unfold_ics_lines(folded)
        self.assertEqual(unfolded, [line])


class TestTextEscapingAndUnescaping(unittest.TestCase):
    """Test RFC 5545 §3.3.11 text escaping and unescaping."""

    def test_unescape_special_characters(self) -> None:
        raw = r"Text with \, comma\, \; semicolon\, \\ backslash and \n newlines and \N capital N."
        unescaped = unescape_ics_text(raw)
        self.assertEqual(
            unescaped,
            "Text with , comma, ; semicolon, \\ backslash and \n newlines and \n capital N.",
        )

    def test_escape_special_characters(self) -> None:
        plain = "A, B; C \\ D\nLine 2\r\nLine 3"
        escaped = escape_ics_text(plain)
        self.assertEqual(escaped, r"A\, B\; C \\ D\nLine 2\nLine 3")

    def test_escape_unescape_roundtrip(self) -> None:
        original = "Projekt: Q4; Status, Überprüfung \\ wichtig!\nZweite Zeile."
        self.assertEqual(unescape_ics_text(escape_ics_text(original)), original)


class TestTimezoneResolution(unittest.TestCase):
    """Test IANA and Windows timezone resolution."""

    def test_windows_tz_aliases_defined(self) -> None:
        self.assertIn("W. Europe Standard Time", WINDOWS_TZ_ALIASES)
        self.assertEqual(WINDOWS_TZ_ALIASES["W. Europe Standard Time"], "Europe/Vienna")
        self.assertEqual(WINDOWS_TZ_ALIASES["Central European Standard Time"], "Europe/Warsaw")
        self.assertEqual(WINDOWS_TZ_ALIASES["Romance Standard Time"], "Europe/Paris")
        self.assertEqual(WINDOWS_TZ_ALIASES["GMT Standard Time"], "Europe/London")
        self.assertEqual(WINDOWS_TZ_ALIASES["Eastern Standard Time"], "America/New_York")
        self.assertEqual(WINDOWS_TZ_ALIASES["Pacific Standard Time"], "America/Los_Angeles")
        self.assertEqual(WINDOWS_TZ_ALIASES["UTC"], "UTC")

    def test_resolve_tzid_success(self) -> None:
        # Standard IANA name
        tz = resolve_tzid("Europe/Vienna")
        self.assertIsInstance(tz, ZoneInfo)
        self.assertEqual(tz.key, "Europe/Vienna")

        # Windows alias
        tz_win = resolve_tzid("W. Europe Standard Time")
        self.assertEqual(tz_win.key, "Europe/Vienna")

        # Default fallback
        tz_none = resolve_tzid(None, default_tz="Europe/Vienna")
        self.assertEqual(tz_none.key, "Europe/Vienna")

    def test_resolve_tzid_unknown_raises(self) -> None:
        with self.assertRaises(ICalCodecError) as ctx:
            resolve_tzid("Moon/Sea_of_Tranquility")
        self.assertEqual(ctx.exception.code, "ICAL_TIMEZONE_ERROR")


class TestParseIcsToRecords(unittest.TestCase):
    """Test parse_ics_to_records on various VEVENT structures."""

    def test_missing_vcalendar_structure_raises(self) -> None:
        raw = "BEGIN:VEVENT\r\nUID:123\r\nEND:VEVENT"
        with self.assertRaises(ICalCodecError) as ctx:
            parse_ics_to_records(raw)
        self.assertEqual(ctx.exception.code, "ICAL_INVALID_STRUCTURE")

    def test_missing_uid_raises(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART:20261110T100000Z\r\n"
            "SUMMARY:Missing UID\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with self.assertRaises(ICalCodecError) as ctx:
            parse_ics_to_records(raw)
        self.assertEqual(ctx.exception.code, "ICAL_MISSING_UID")

    def test_missing_dtstart_raises(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:no-dtstart-1\r\n"
            "SUMMARY:No DTSTART\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with self.assertRaises(ICalCodecError) as ctx:
            parse_ics_to_records(raw)
        self.assertEqual(ctx.exception.code, "ICAL_MISSING_DTSTART")

    def test_valarm_does_not_clobber_vevent_properties(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:valarm-clobber-test-001\r\n"
            "DTSTART:20261110T100000Z\r\n"
            "DTEND:20261110T110000Z\r\n"
            "SUMMARY:Actual Event Summary\r\n"
            "DESCRIPTION:Actual Event Description\r\n"
            "BEGIN:VALARM\r\n"
            "ACTION:DISPLAY\r\n"
            "DESCRIPTION:Reminder: 15 minutes prior\r\n"
            "SUMMARY:Alarm Summary That Must Not Clobber\r\n"
            "TRIGGER:-PT15M\r\n"
            "END:VALARM\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        records = parse_ics_to_records(raw)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["summary"], "Actual Event Summary")
        self.assertEqual(rec["description"], "Actual Event Description")

    def test_duration_property_handled_when_dtend_absent(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:duration-test-001\r\n"
            "DTSTART:20261110T100000Z\r\n"
            "DURATION:PT1H30M\r\n"
            "SUMMARY:Event with Duration\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        records = parse_ics_to_records(raw)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["duration_minutes"], 90)
        self.assertEqual(rec["end"]["utc"], "2026-11-10T11:30:00Z")

    def test_fixture_timed_event_conformance(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            fixtures = json.load(f)["fixtures"]
        timed = fixtures["timed_event"]
        records = parse_ics_to_records(
            timed["raw_ics"],
            account="infomaniak-work",
            calendar_alias="primary",
        )
        self.assertEqual(len(records), 1)
        rec = records[0]
        exp = timed["expected_record"]
        self.assertEqual(rec["uid"], exp["uid"])
        self.assertEqual(rec["summary"], exp["summary"])
        self.assertEqual(rec["start"], exp["start"])
        self.assertEqual(rec["end"], exp["end"])
        self.assertEqual(rec["duration_minutes"], exp["duration_minutes"])
        self.assertEqual(rec["content_sha256"], exp["content_sha256"])

    def test_fixture_all_day_event_conformance(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            fixtures = json.load(f)["fixtures"]
        allday = fixtures["all_day_event"]
        records = parse_ics_to_records(
            allday["raw_ics"],
            account="infomaniak-work",
            calendar_alias="primary",
        )
        self.assertEqual(len(records), 1)
        rec = records[0]
        exp = allday["expected_record"]
        self.assertTrue(rec["start"]["all_day"])
        self.assertTrue(rec["end"]["all_day"])
        self.assertIsNone(rec["start"]["utc"])
        self.assertEqual(rec["start"]["date"], "2026-12-24")
        self.assertEqual(rec["end"]["date"], "2026-12-27")
        self.assertEqual(rec["duration_minutes"], 4320)
        self.assertEqual(rec["content_sha256"], exp["content_sha256"])

    def test_fixture_meeting_request_conformance(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            fixtures = json.load(f)["fixtures"]
        meeting = fixtures["meeting_request"]
        records = parse_ics_to_records(meeting["raw_ics"])
        self.assertEqual(len(records), 1)
        rec = records[0]
        exp = meeting["expected_record"]
        self.assertEqual(rec["method"], "REQUEST")
        self.assertEqual(rec["organizer"], exp["organizer"])
        self.assertEqual(rec["attendees"], exp["attendees"])

    def test_parse_ics_x_wr_timezone_fallback(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "X-WR-TIMEZONE:America/New_York\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:floating-001\r\n"
            "DTSTART:20261110T100000\r\n"
            "DTEND:20261110T110000\r\n"
            "SUMMARY:Floating Event Using X-WR-TIMEZONE\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        records = parse_ics_to_records(raw)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["start"]["tzid"], "America/New_York")
        # In America/New_York in Nov (EST = UTC-5): 10:00 local is 15:00 UTC
        self.assertEqual(rec["start"]["utc"], "2026-11-10T15:00:00Z")


class TestExtractCalendarObjectRef(unittest.TestCase):
    """Test extract_calendar_object_ref on record dicts and raw strings."""

    def test_extract_from_record(self) -> None:
        record = {
            "uid": "event-123",
            "sequence": 2,
            "recurrence_id": "2026-11-10T10:00:00Z",
            "content_sha256": "abcdef1234567890" * 4,
            "summary": "Sample",
        }
        ref = extract_calendar_object_ref(record)
        self.assertEqual(
            ref,
            {
                "uid": "event-123",
                "sequence": 2,
                "recurrence_id": "2026-11-10T10:00:00Z",
                "content_sha256": "abcdef1234567890" * 4,
            },
        )

    def test_extract_from_raw_ics(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            fixtures = json.load(f)["fixtures"]
        raw = fixtures["timed_event"]["raw_ics"]
        ref = extract_calendar_object_ref(raw)
        exp = fixtures["timed_event"]["expected_calendar_object_ref"]
        self.assertEqual(ref["uid"], exp["uid"])
        self.assertEqual(ref["sequence"], exp["sequence"])
        self.assertEqual(ref["content_sha256"], exp["content_sha256"])


class TestRecurrenceExpansion(unittest.TestCase):
    """Test expansion of RRULE, EXDATE, override records, and DST transitions."""

    def test_non_recurring_event_expansion(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            fixtures = json.load(f)["fixtures"]
        timed = fixtures["timed_event"]["expected_record"]

        # Within window
        occs = expand_record_occurrences(
            timed,
            window_start_utc="2026-11-10T00:00:00Z",
            window_end_utc="2026-11-11T00:00:00Z",
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0]["uid"], timed["uid"])

        # Outside window
        outside = expand_record_occurrences(
            timed,
            window_start_utc="2026-11-12T00:00:00Z",
            window_end_utc="2026-11-13T00:00:00Z",
        )
        self.assertEqual(len(outside), 0)

    def test_rrule_with_exdate_and_override_expansion(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            rec_fixture = json.load(f)["fixtures"]["recurring_event"]
        master = rec_fixture["expected_master_record"]
        override = rec_fixture["expected_override_record"]
        exp_plan = rec_fixture["recurrence_expansion"]

        occs = expand_record_occurrences(
            master,
            window_start_utc=exp_plan["window_start_utc"],
            window_end_utc=exp_plan["window_end_utc"],
            override_records=[override],
        )

        self.assertEqual(len(occs), exp_plan["expected_occurrence_count"])
        start_utcs = [occ["start"]["utc"] for occ in occs]
        self.assertEqual(start_utcs, exp_plan["expected_start_utc_list"])

        # Check that 2026-11-09 occurrence has the override summary and start time
        nov9_occ = [occ for occ in occs if occ["start"]["date"] == "2026-11-09"][0]
        self.assertEqual(nov9_occ["summary"], override["summary"])
        self.assertEqual(nov9_occ["start"]["local"], "2026-11-09T14:00:00+01:00")

    def test_cancelled_override_removes_instance(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            rec_fixture = json.load(f)["fixtures"]["recurring_event"]
        master = rec_fixture["expected_master_record"]
        cancelled_override = dict(rec_fixture["expected_override_record"])
        cancelled_override["status"] = "CANCELLED"

        occs = expand_record_occurrences(
            master,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-20T00:00:00Z",
            override_records=[cancelled_override],
        )

        # 5 instances minus the cancelled Nov 9 instance = 4 instances
        self.assertEqual(len(occs), 4)
        dates = [occ["start"]["date"] for occ in occs]
        self.assertNotIn("2026-11-09", dates)

    def test_dst_transition_preserves_local_wall_clock(self) -> None:
        # Europe/Vienna transitions from CEST (UTC+2) to CET (UTC+1) on Sunday, 2026-10-25.
        # Event every Tuesday at 10:00 local time:
        # - Tue 2026-10-20 is CEST (UTC+2) -> 08:00Z
        # - Tue 2026-10-27 is CET (UTC+1)  -> 09:00Z
        record = {
            "schema": "calendar-record/1",
            "uid": "weekly-dst-test@office-intelligence.test",
            "start": {
                "utc": "2026-10-20T08:00:00Z",
                "local": "2026-10-20T10:00:00+02:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-10-20",
            },
            "end": {
                "utc": "2026-10-20T09:00:00Z",
                "local": "2026-10-20T11:00:00+02:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-10-20",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=WEEKLY;COUNT=2",
            "exdates": [],
            "summary": "Weekly Tuesday Standup",
        }

        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-10-15T00:00:00Z",
            window_end_utc="2026-11-01T00:00:00Z",
        )

        self.assertEqual(len(occs), 2)
        # Pre-transition
        self.assertEqual(occs[0]["start"]["local"], "2026-10-20T10:00:00+02:00")
        self.assertEqual(occs[0]["start"]["utc"], "2026-10-20T08:00:00Z")
        # Post-transition: local time is still 10:00!
        self.assertEqual(occs[1]["start"]["local"], "2026-10-27T10:00:00+01:00")
        self.assertEqual(occs[1]["start"]["utc"], "2026-10-27T09:00:00Z")

    def test_monthly_byday_expansion(self) -> None:
        # First Monday of the month: BYDAY=1MO
        record = {
            "schema": "calendar-record/1",
            "uid": "monthly-1mo@office-intelligence.test",
            "start": {
                "utc": "2026-11-02T08:00:00Z",
                "local": "2026-11-02T09:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-02",
            },
            "end": {
                "utc": "2026-11-02T09:00:00Z",
                "local": "2026-11-02T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-02",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=MONTHLY;BYDAY=1MO;COUNT=2",
            "exdates": [],
            "summary": "Monthly All-Hands",
        }
        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2027-01-01T00:00:00Z",
        )
        self.assertEqual(len(occs), 2)
        self.assertEqual(occs[0]["start"]["date"], "2026-11-02")
        self.assertEqual(occs[1]["start"]["date"], "2026-12-07")  # 1st Monday in Dec 2026 is Dec 7

    def test_daily_recurrence_expansion(self) -> None:
        record = {
            "schema": "calendar-record/1",
            "uid": "daily-001@test",
            "start": {
                "utc": "2026-11-01T08:00:00Z",
                "local": "2026-11-01T09:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-01",
            },
            "end": {
                "utc": "2026-11-01T09:00:00Z",
                "local": "2026-11-01T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-01",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=DAILY;INTERVAL=2;COUNT=4",
            "exdates": [],
            "summary": "Daily Standup",
        }
        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-10T00:00:00Z",
        )
        self.assertEqual(len(occs), 4)
        dates = [occ["start"]["date"] for occ in occs]
        self.assertEqual(dates, ["2026-11-01", "2026-11-03", "2026-11-05", "2026-11-07"])

    def test_yearly_recurrence_expansion(self) -> None:
        record = {
            "schema": "calendar-record/1",
            "uid": "yearly-001@test",
            "start": {
                "utc": "2026-06-15T08:00:00Z",
                "local": "2026-06-15T10:00:00+02:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-06-15",
            },
            "end": {
                "utc": "2026-06-15T09:00:00Z",
                "local": "2026-06-15T11:00:00+02:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-06-15",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=YEARLY;COUNT=3",
            "exdates": [],
            "summary": "Annual Strategy Day",
        }
        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2029-01-01T00:00:00Z",
        )
        self.assertEqual(len(occs), 3)
        years = [occ["start"]["date"][:4] for occ in occs]
        self.assertEqual(years, ["2026", "2027", "2028"])

    def test_monthly_with_exdate_and_override_and_until(self) -> None:
        record = {
            "schema": "calendar-record/1",
            "uid": "monthly-adv-001@test",
            "start": {
                "utc": "2026-01-15T09:00:00Z",
                "local": "2026-01-15T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-01-15",
            },
            "end": {
                "utc": "2026-01-15T10:00:00Z",
                "local": "2026-01-15T11:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-01-15",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=MONTHLY;UNTIL=20260501T000000Z",
            "exdates": ["2026-02-15T09:00:00Z"],
            "summary": "Monthly Sync",
        }
        override = copy.deepcopy(record)
        override["recurrence_id"] = "2026-03-15T09:00:00Z"
        override["summary"] = "Monthly Sync (Q1 Special)"
        override["start"]["date"] = "2026-03-15"
        override["start"]["utc"] = "2026-03-15T09:00:00Z"
        override["start"]["local"] = "2026-03-15T10:00:00+01:00"
        override["end"]["date"] = "2026-03-15"
        override["end"]["utc"] = "2026-03-15T10:00:00Z"
        override["end"]["local"] = "2026-03-15T11:00:00+01:00"

        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2026-06-01T00:00:00Z",
            override_records=[override],
        )
        # Jan 15 (normal), Feb 15 (exdate), Mar 15 (override), Apr 15 (normal). May 15 is after UNTIL (May 1).
        self.assertEqual(len(occs), 3)
        self.assertEqual(occs[0]["start"]["date"], "2026-01-15")
        self.assertEqual(occs[1]["start"]["date"], "2026-03-15")
        self.assertEqual(occs[1]["summary"], "Monthly Sync (Q1 Special)")
        self.assertEqual(occs[2]["start"]["date"], "2026-04-15")

    def test_unbounded_daily_and_weekly_recurrence(self) -> None:
        # Unbounded DAILY event (no COUNT, no UNTIL)
        daily_record = {
            "schema": "calendar-record/1",
            "uid": "unbounded-daily@office-intelligence.test",
            "start": {
                "utc": "2026-11-01T08:00:00Z",
                "local": "2026-11-01T09:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-01",
            },
            "end": {
                "utc": "2026-11-01T09:00:00Z",
                "local": "2026-11-01T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-01",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=DAILY",
            "exdates": [],
            "summary": "Unbounded Daily Standup",
        }
        # 14-day window: Nov 1 to Nov 15
        daily_occs = expand_record_occurrences(
            daily_record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-15T00:00:00Z",
        )
        self.assertEqual(len(daily_occs), 14)
        daily_dates = [occ["start"]["date"] for occ in daily_occs]
        self.assertEqual(daily_dates[0], "2026-11-01")
        self.assertEqual(daily_dates[-1], "2026-11-14")

        # Unbounded WEEKLY event (no COUNT, no UNTIL)
        weekly_record = {
            "schema": "calendar-record/1",
            "uid": "unbounded-weekly@office-intelligence.test",
            "start": {
                "utc": "2026-11-02T08:00:00Z",
                "local": "2026-11-02T09:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-02",
            },
            "end": {
                "utc": "2026-11-02T09:00:00Z",
                "local": "2026-11-02T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-02",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "exdates": [],
            "summary": "Unbounded Weekly Monday",
        }
        # Window: Nov 1 to Dec 1 (Mondays: Nov 2, 9, 16, 23, 30 = 5 occurrences)
        weekly_occs = expand_record_occurrences(
            weekly_record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-12-01T00:00:00Z",
        )
        self.assertEqual(len(weekly_occs), 5)
        weekly_dates = [occ["start"]["date"] for occ in weekly_occs]
        self.assertEqual(
            weekly_dates,
            ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"],
        )

    def test_monthly_unsorted_byday_chronological_order(self) -> None:
        # BYDAY=-1FR,1MO where -1FR is earlier in list but later in month than 1MO
        record = {
            "schema": "calendar-record/1",
            "uid": "monthly-unsorted-byday@office-intelligence.test",
            "start": {
                "utc": "2026-10-01T08:00:00Z",
                "local": "2026-10-01T10:00:00+02:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-10-01",
            },
            "end": {
                "utc": "2026-10-01T09:00:00Z",
                "local": "2026-10-01T11:00:00+02:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-10-01",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=MONTHLY;BYDAY=-1FR,1MO",
            "exdates": [],
            "summary": "Monthly Start and End",
        }
        # In October 2026:
        # 1st Monday: 2026-10-05
        # Last Friday: 2026-10-30
        # Window Oct 1 to Oct 15 includes 1MO but not -1FR
        occs_half = expand_record_occurrences(
            record,
            window_start_utc="2026-10-01T00:00:00Z",
            window_end_utc="2026-10-15T00:00:00Z",
        )
        self.assertEqual(len(occs_half), 1)
        self.assertEqual(occs_half[0]["start"]["date"], "2026-10-05")

        # Full month window includes both in chronological order
        occs_full = expand_record_occurrences(
            record,
            window_start_utc="2026-10-01T00:00:00Z",
            window_end_utc="2026-11-01T00:00:00Z",
        )
        self.assertEqual(len(occs_full), 2)
        self.assertEqual(occs_full[0]["start"]["date"], "2026-10-05")
        self.assertEqual(occs_full[1]["start"]["date"], "2026-10-30")

    def test_unbounded_recurrence_with_override_past_window(self) -> None:
        # Weekly event on Mondays; search window Nov 1 to Nov 10 (contains Nov 2, Nov 9)
        # Nov 16 instance is overridden and rescheduled earlier to Nov 5 (inside window)
        record = {
            "schema": "calendar-record/1",
            "uid": "weekly-ovr-past@office-intelligence.test",
            "start": {
                "utc": "2026-11-02T08:00:00Z",
                "local": "2026-11-02T09:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-02",
            },
            "end": {
                "utc": "2026-11-02T09:00:00Z",
                "local": "2026-11-02T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-02",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "exdates": [],
            "summary": "Weekly Planning",
        }
        override = copy.deepcopy(record)
        override["recurrence_id"] = "2026-11-16T08:00:00Z"
        override["summary"] = "Weekly Planning (Rescheduled to Nov 5)"
        override["start"]["date"] = "2026-11-05"
        override["start"]["utc"] = "2026-11-05T08:00:00Z"
        override["start"]["local"] = "2026-11-05T09:00:00+01:00"
        override["end"]["date"] = "2026-11-05"
        override["end"]["utc"] = "2026-11-05T09:00:00Z"
        override["end"]["local"] = "2026-11-05T10:00:00+01:00"

        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-10T00:00:00Z",
            override_records=[override],
        )
        # Should contain: Nov 2, Nov 5 (rescheduled from Nov 16), Nov 9
        self.assertEqual(len(occs), 3)
        dates = [occ["start"]["date"] for occ in occs]
        self.assertEqual(dates, ["2026-11-02", "2026-11-05", "2026-11-09"])

    def test_invalid_byday_raises_codec_error(self) -> None:
        record = {
            "schema": "calendar-record/1",
            "uid": "bad-byday@test",
            "start": {
                "utc": "2026-11-01T08:00:00Z",
                "local": "2026-11-01T09:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-01",
            },
            "end": {
                "utc": "2026-11-01T09:00:00Z",
                "local": "2026-11-01T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-01",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=WEEKLY;BYDAY=INVALID",
            "summary": "Bad Byday",
        }
        with self.assertRaises(ICalCodecError) as ctx:
            expand_record_occurrences(
                record,
                window_start_utc="2026-11-01T00:00:00Z",
                window_end_utc="2026-11-30T00:00:00Z",
            )
        self.assertEqual(ctx.exception.code, "ICAL_INVALID_RRULE")


class TestSerializationAndLosslessUpdate(unittest.TestCase):
    """Test build_vevent_ics and lossless update_vevent_ics."""

    def test_build_vevent_ics_produces_compliant_vcalendar(self) -> None:
        with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
            timed = json.load(f)["fixtures"]["timed_event"]["expected_record"]
        raw_built = build_vevent_ics(timed)
        self.assertTrue(raw_built.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(raw_built.endswith("END:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0\r\n", raw_built)
        self.assertIn("PRODID:-//office-intelligence//calendar-desk//EN\r\n", raw_built)
        self.assertIn(f"UID:{timed['uid']}\r\n", raw_built)

        # Parse it back and verify equivalence
        reparsed = parse_ics_to_records(raw_built)
        self.assertEqual(len(reparsed), 1)
        self.assertEqual(reparsed[0]["uid"], timed["uid"])
        self.assertEqual(reparsed[0]["summary"], timed["summary"])

    def test_update_vevent_ics_preserves_extensions_and_valarm(self) -> None:
        raw_original = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//custom//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:update-test-001\r\n"
            "SEQUENCE:3\r\n"
            "DTSTART:20261110T090000Z\r\n"
            "DTEND:20261110T100000Z\r\n"
            "SUMMARY:Original Title\r\n"
            "X-SPECIAL-CUSTOM-TAG:PRESERVE-THIS-VALUE\r\n"
            "BEGIN:VALARM\r\n"
            "ACTION:DISPLAY\r\n"
            "TRIGGER:-PT10M\r\n"
            "DESCRIPTION:Reminder\r\n"
            "END:VALARM\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

        updates = {
            "summary": "Updated Title For Review",
            "location": "Room 404",
        }

        updated_ics = update_vevent_ics(raw_original, updates, increment_sequence=True)

        self.assertIn("SUMMARY:Updated Title For Review\r\n", updated_ics)
        self.assertIn("LOCATION:Room 404\r\n", updated_ics)
        # Sequence incremented from 3 to 4
        self.assertIn("SEQUENCE:4\r\n", updated_ics)
        # Extension property preserved
        self.assertIn("X-SPECIAL-CUSTOM-TAG:PRESERVE-THIS-VALUE\r\n", updated_ics)
        # VALARM preserved
        self.assertIn("BEGIN:VALARM\r\n", updated_ics)
        self.assertIn("TRIGGER:-PT10M\r\n", updated_ics)
        self.assertIn("END:VALARM\r\n", updated_ics)

    def test_update_vevent_ics_on_folded_input(self) -> None:
        raw_folded = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:fold-update-001\r\n"
            "SUMMARY:Original\r\n"
            "DESCRIPTION:This is a very long line that was previously\r\n"
            "  folded across multiple physical lines in the source\r\n"
            "DTSTART:20261110T090000Z\r\n"
            "DTEND:20261110T100000Z\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        updated = update_vevent_ics(
            raw_folded,
            {"description": "Clean short description"},
            increment_sequence=True,
        )
        self.assertIn("DESCRIPTION:Clean short description\r\n", updated)
        self.assertNotIn("folded across multiple", updated)
        self.assertIn("SEQUENCE:1\r\n", updated)

    def test_update_vevent_ics_start_end_rrule(self) -> None:
        raw = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:se-update-001\r\n"
            "DTSTART:20261110T090000Z\r\n"
            "DTEND:20261110T100000Z\r\n"
            "SUMMARY:Old Time\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        new_start = {
            "utc": "2026-11-12T13:00:00Z",
            "local": "2026-11-12T14:00:00+01:00",
            "tzid": "Europe/Vienna",
            "all_day": False,
            "date": "2026-11-12",
        }
        new_end = {
            "utc": "2026-11-12T14:30:00Z",
            "local": "2026-11-12T15:30:00+01:00",
            "tzid": "Europe/Vienna",
            "all_day": False,
            "date": "2026-11-12",
        }
        updated = update_vevent_ics(
            raw,
            {
                "start": new_start,
                "end": new_end,
                "rrule": "FREQ=WEEKLY;COUNT=5",
                "location": "Boardroom",
            },
            increment_sequence=True,
        )
        self.assertIn("DTSTART;TZID=Europe/Vienna:20261112T140000\r\n", updated)
        self.assertIn("DTEND;TZID=Europe/Vienna:20261112T153000\r\n", updated)
        self.assertIn("RRULE:FREQ=WEEKLY;COUNT=5\r\n", updated)
        self.assertIn("LOCATION:Boardroom\r\n", updated)


class TestCodecSecurityAndRfcRemediation(unittest.TestCase):
    """PR #82 Review Remediation tests: Codec & Serialization Security (REV-2)."""

    def setUp(self) -> None:
        self.base_record = {
            "schema": "calendar-record/1",
            "uid": "rev2-test-001@office-intelligence.local",
            "sequence": 0,
            "status": "CONFIRMED",
            "transp": "OPAQUE",
            "classification": "PUBLIC",
            "start": {
                "utc": "2026-11-10T09:00:00Z",
                "local": "2026-11-10T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-10",
            },
            "end": {
                "utc": "2026-11-10T10:00:00Z",
                "local": "2026-11-10T11:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-10",
            },
            "summary": "Codec Security Review Meeting",
            "location": "Room 101",
            "duration_minutes": 60,
        }

    def test_serializer_rejects_crlf_property_injection(self) -> None:
        """Verify build_vevent_ics rejects CR/LF injection and escape_ics_text normalizes isolated CR."""
        # 1. build_vevent_ics must reject \r and \n in unescaped properties
        injection_fields = [
            ("uid", "legit-uid\r\nSTATUS:CANCELLED"),
            ("uid", "legit-uid\nINJECTED:TRUE"),
            ("status", "CONFIRMED\r\nSUMMARY:Injected"),
            ("transp", "OPAQUE\nUID:fake-uid"),
            ("classification", "PUBLIC\rX-INJECT:true"),
            ("rrule", "FREQ=DAILY\r\nATTENDEE:mailto:evil@example.com"),
        ]
        for field, evil_value in injection_fields:
            bad_record = copy.deepcopy(self.base_record)
            bad_record[field] = evil_value
            with self.assertRaises(ICalCodecError, msg=f"Should reject injection in {field}") as ctx:
                build_vevent_ics(bad_record)
            self.assertIn(
                ctx.exception.code,
                ("ICAL_INJECTION_DETECTED", "ICAL_CONTROL_CHAR"),
                f"Expected injection error code for field {field}, got {ctx.exception.code}",
            )

        # 2. escape_ics_text must normalize isolated \r so unfold_ics_lines sees a single line
        isolated_cr_text = "Line1\rLine2\rLine3"
        escaped = escape_ics_text(isolated_cr_text)
        self.assertNotIn("\r", escaped, "escape_ics_text must not leave raw isolated \\r in escaped output")
        self.assertIn(r"\n", escaped)
        raw_ics_line = f"SUMMARY:{escaped}\r\n"
        unfolded = unfold_ics_lines(raw_ics_line)
        self.assertEqual(len(unfolded), 1, "Unfolding text with isolated CR must not split into multiple lines")

    def test_serializer_quotes_cn_parameters_with_dquote(self) -> None:
        """Verify build_vevent_ics encloses parameter values containing colons/semicolons/commas in DQUOTEs."""
        record = copy.deepcopy(self.base_record)
        record["organizer"] = {
            "cn": "Smith: John, Jr.",
            "cal_address": "mailto:jsmith@example.com",
        }
        record["attendees"] = [
            {
                "cn": "Doe, Jane; Dr.",
                "cal_address": "mailto:jdoe@example.com",
                "partstat": "ACCEPTED",
            }
        ]
        ics = build_vevent_ics(record)
        # Check ORGANIZER line has CN in DQUOTEs per RFC 5545 §3.1
        self.assertIn(
            'ORGANIZER;CN="Smith: John, Jr.":mailto:jsmith@example.com',
            ics,
            "Organizer CN with colon/comma must be enclosed in DQUOTEs",
        )
        # Check ATTENDEE line has CN in DQUOTEs
        self.assertIn(
            'CN="Doe, Jane; Dr."',
            ics,
            "Attendee CN with semicolon/comma must be enclosed in DQUOTEs",
        )

        # Roundtrip parse test: ensure quotes are stripped and original CN restored cleanly
        parsed = parse_ics_to_records(ics)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["organizer"]["cn"], "Smith: John, Jr.")
        self.assertEqual(parsed[0]["attendees"][0]["cn"], "Doe, Jane; Dr.")

    def test_build_vevent_emits_dtstamp_and_rejects_inverted_dates(self) -> None:
        """Verify build_vevent_ics emits mandatory RFC-5545 DTSTAMP and rejects end < start."""
        # 1. Must emit DTSTAMP
        ics = build_vevent_ics(self.base_record)
        self.assertRegex(
            ics,
            r"DTSTAMP:\d{8}T\d{6}Z\r\n",
            "build_vevent_ics must emit RFC-5545 mandatory DTSTAMP timestamp",
        )

        # 2. end < start for timed events must fail closed
        bad_timed = copy.deepcopy(self.base_record)
        bad_timed["start"]["utc"] = "2026-11-10T12:00:00Z"
        bad_timed["start"]["local"] = "2026-11-10T13:00:00+01:00"
        bad_timed["end"]["utc"] = "2026-11-10T10:00:00Z"
        bad_timed["end"]["local"] = "2026-11-10T11:00:00+01:00"
        with self.assertRaises(ICalCodecError) as ctx:
            build_vevent_ics(bad_timed)
        self.assertEqual(ctx.exception.code, "ICAL_INVALID_DATETIME")

        # 3. end < start for all-day events must fail closed
        bad_all_day = copy.deepcopy(self.base_record)
        bad_all_day["start"] = {
            "all_day": True,
            "date": "2026-11-15",
            "utc": None,
            "local": None,
            "tzid": None,
        }
        bad_all_day["end"] = {
            "all_day": True,
            "date": "2026-11-10",
            "utc": None,
            "local": None,
            "tzid": None,
        }
        with self.assertRaises(ICalCodecError) as ctx_allday:
            build_vevent_ics(bad_all_day)
        self.assertEqual(ctx_allday.exception.code, "ICAL_INVALID_DATETIME")

    def test_update_vevent_updates_last_modified_and_targets_recurrence_id(self) -> None:
        """Verify update_vevent_ics maintains LAST-MODIFIED and supports recurrence_id instance targeting."""
        multi_vevent_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//office-intelligence//test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-override-001\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261102T080000Z\r\n"
            "DTEND:20261102T090000Z\r\n"
            "SUMMARY:Master Recurring Event\r\n"
            "RRULE:FREQ=WEEKLY\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-override-001\r\n"
            "RECURRENCE-ID:20261109T080000Z\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261109T100000Z\r\n"
            "DTEND:20261109T110000Z\r\n"
            "SUMMARY:Original Override Exception\r\n"
            "LAST-MODIFIED:20261101T000000Z\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

        # 1. Update the master event: LAST-MODIFIED must be present/updated
        updated_master = update_vevent_ics(
            multi_vevent_ics,
            {"summary": "Updated Master Event"},
            increment_sequence=True,
        )
        self.assertIn("SUMMARY:Updated Master Event\r\n", updated_master)
        self.assertIn("SUMMARY:Original Override Exception\r\n", updated_master)
        self.assertRegex(
            updated_master,
            r"LAST-MODIFIED:\d{8}T\d{6}Z",
            "update_vevent_ics must add or update LAST-MODIFIED timestamp",
        )

        # 2. Targeted update for specific RECURRENCE-ID instance
        updated_instance = update_vevent_ics(
            multi_vevent_ics,
            {"summary": "Rescheduled Exception Meeting"},
            recurrence_id="2026-11-09T08:00:00Z",
            increment_sequence=True,
        )
        # Master must stay untouched
        self.assertIn("SUMMARY:Master Recurring Event\r\n", updated_instance)
        # Targeted instance must have new summary
        self.assertIn("SUMMARY:Rescheduled Exception Meeting\r\n", updated_instance)
        self.assertNotIn("SUMMARY:Original Override Exception\r\n", updated_instance)

    def test_rrule_byday_ordinal_bounds_and_infinite_loop_prevention(self) -> None:
        """Verify out-of-bound BYDAY ordinals fail-closed without hanging recurrence expansion."""
        def run_with_timeout(func, timeout_sec: float = 1.0):
            result = []
            exc = []
            def target():
                try:
                    result.append(func())
                except Exception as e:
                    exc.append(e)
            t = threading.Thread(target=target, daemon=True)
            t.start()
            t.join(timeout_sec)
            if t.is_alive():
                raise AssertionError("Infinite loop detected: recurrence expansion timed out")
            if exc:
                raise exc[0]
            return result[0]

        bad_byday_cases = [
            ("FREQ=MONTHLY;BYDAY=6MO", "Monthly BYDAY ordinal 6 out of range (max 5)"),
            ("FREQ=MONTHLY;BYDAY=-6FR", "Monthly BYDAY ordinal -6 out of range (min -5)"),
            ("FREQ=YEARLY;BYDAY=0MO", "Yearly BYDAY ordinal 0 is invalid"),
            ("FREQ=WEEKLY;BYDAY=1MO", "Weekly BYDAY must not specify numeric ordinal prefix"),
        ]

        for rrule_str, desc in bad_byday_cases:
            rec = copy.deepcopy(self.base_record)
            rec["rrule"] = rrule_str
            with self.assertRaises(ICalCodecError, msg=f"Should reject {desc}") as ctx:
                run_with_timeout(
                    lambda: expand_record_occurrences(
                        rec,
                        window_start_utc="2026-11-01T00:00:00Z",
                        window_end_utc="2026-11-30T00:00:00Z",
                    ),
                    timeout_sec=1.0,
                )
            self.assertEqual(
                ctx.exception.code,
                "ICAL_INVALID_RRULE",
                f"Expected ICAL_INVALID_RRULE for {desc}, got {ctx.exception.code}",
            )

    def test_rrule_unhandled_parts_fail_closed_and_daily_weekdays(self) -> None:
        """Verify unhandled RRULE parts raise ICAL_UNSUPPORTED_RRULE and FREQ=DAILY;BYDAY expands weekdays only."""
        # 1. Unhandled RRULE parts must fail-closed with ICAL_UNSUPPORTED_RRULE
        unsupported_rrules = [
            "FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1",
            "FREQ=MONTHLY;BYMONTHDAY=2,16",
            "FREQ=YEARLY;BYWEEKNO=20",
            "FREQ=DAILY;BYHOUR=9,17",
        ]
        for rrule_str in unsupported_rrules:
            rec = copy.deepcopy(self.base_record)
            rec["rrule"] = rrule_str
            with self.assertRaises(ICalCodecError, msg=f"Should raise for unsupported part in {rrule_str}") as ctx:
                expand_record_occurrences(
                    rec,
                    window_start_utc="2026-11-01T00:00:00Z",
                    window_end_utc="2026-11-30T00:00:00Z",
                )
            self.assertEqual(
                ctx.exception.code,
                "ICAL_UNSUPPORTED_RRULE",
                f"Expected ICAL_UNSUPPORTED_RRULE for {rrule_str}, got {ctx.exception.code}",
            )

        # 2. FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR must only expand weekdays
        weekday_rec = copy.deepcopy(self.base_record)
        # Friday 2026-11-06
        weekday_rec["start"]["utc"] = "2026-11-06T09:00:00Z"
        weekday_rec["start"]["local"] = "2026-11-06T10:00:00+01:00"
        weekday_rec["start"]["date"] = "2026-11-06"
        weekday_rec["end"]["utc"] = "2026-11-06T10:00:00Z"
        weekday_rec["end"]["local"] = "2026-11-06T11:00:00+01:00"
        weekday_rec["end"]["date"] = "2026-11-06"
        weekday_rec["rrule"] = "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"

        occs = expand_record_occurrences(
            weekday_rec,
            window_start_utc="2026-11-06T00:00:00Z",
            window_end_utc="2026-11-14T00:00:00Z",
        )
        occ_dates = [occ["start"]["date"] for occ in occs]
        # Should include Nov 6 (Fri), Nov 9 (Mon), Nov 10 (Tue), Nov 11 (Wed), Nov 12 (Thu), Nov 13 (Fri)
        self.assertNotIn("2026-11-07", occ_dates, "Saturday must not be generated for DAILY;BYDAY=MO..FR")
        self.assertNotIn("2026-11-08", occ_dates, "Sunday must not be generated for DAILY;BYDAY=MO..FR")
        self.assertEqual(
            occ_dates,
            ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"],
        )


class TestRev8CodecRemediations(unittest.TestCase):
    """PR #82 Review Remediation tests: Codec & Serialization Hardening (REV-8)."""

    def setUp(self) -> None:
        self.base_record = {
            "schema": "calendar-record/1",
            "account": "primary-account",
            "calendar_alias": "primary",
            "uid": "rev8-test-001@office-intelligence.local",
            "sequence": 0,
            "status": "CONFIRMED",
            "transp": "OPAQUE",
            "classification": "PUBLIC",
            "start": {
                "utc": "2026-11-10T09:00:00Z",
                "local": "2026-11-10T10:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-10",
            },
            "end": {
                "utc": "2026-11-10T10:00:00Z",
                "local": "2026-11-10T11:00:00+01:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
                "date": "2026-11-10",
            },
            "summary": "REV-8 Test Meeting",
            "location": "Conference Room A",
            "duration_minutes": 60,
        }

    def test_update_vevent_ics_missing_recurrence_id_raises(self) -> None:
        """Verify update_vevent_ics raises ICAL_INSTANCE_NOT_FOUND when recurrence_id is missing,
        and matches ISO-8601 recurrence_id against compact RECURRENCE-ID."""
        raw_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//office-intelligence//test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-1\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261101T100000Z\r\n"
            "DTEND:20261101T110000Z\r\n"
            "SUMMARY:Master Series Meeting\r\n"
            "RRULE:FREQ=WEEKLY\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-1\r\n"
            "RECURRENCE-ID:20261105T100000Z\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261105T100000Z\r\n"
            "DTEND:20261105T110000Z\r\n"
            "SUMMARY:Existing Override Meeting\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        # Non-existent ISO-8601 recurrence_id must raise ICAL_INSTANCE_NOT_FOUND
        with self.assertRaises(ICalCodecError) as ctx_iso:
            update_vevent_ics(
                raw_ics,
                {"summary": "Nope"},
                recurrence_id="2026-11-12T10:00:00Z",
            )
        self.assertEqual(ctx_iso.exception.code, "ICAL_INSTANCE_NOT_FOUND")

        # Non-existent compact recurrence_id must raise ICAL_INSTANCE_NOT_FOUND
        with self.assertRaises(ICalCodecError) as ctx_compact:
            update_vevent_ics(
                raw_ics,
                {"summary": "Nope"},
                recurrence_id="20261112T100000Z",
            )
        self.assertEqual(ctx_compact.exception.code, "ICAL_INSTANCE_NOT_FOUND")

        # Matching ISO-8601 recurrence_id must update ONLY the override VEVENT
        updated = update_vevent_ics(
            raw_ics,
            {"summary": "Updated Override Meeting"},
            recurrence_id="2026-11-05T10:00:00Z",
        )
        self.assertIn("SUMMARY:Master Series Meeting\r\n", updated)
        self.assertIn("SUMMARY:Updated Override Meeting\r\n", updated)
        self.assertNotIn("SUMMARY:Existing Override Meeting\r\n", updated)

    def test_cancel_master_vevent_cascades_to_overrides(self) -> None:
        """Verify cancelling the master event cascades STATUS:CANCELLED to all override VEVENTs."""
        raw_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//office-intelligence//test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-cascade-1\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261101T100000Z\r\n"
            "DTEND:20261101T110000Z\r\n"
            "SUMMARY:Master Series Meeting\r\n"
            "STATUS:CONFIRMED\r\n"
            "RRULE:FREQ=WEEKLY\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:series-cascade-1\r\n"
            "RECURRENCE-ID:20261105T100000Z\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261105T100000Z\r\n"
            "DTEND:20261105T110000Z\r\n"
            "SUMMARY:Override Meeting\r\n"
            "STATUS:CONFIRMED\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        updated = update_vevent_ics(
            raw_ics,
            {"status": "CANCELLED"},
            recurrence_id=None,
        )
        self.assertNotIn("STATUS:CONFIRMED", updated)
        self.assertEqual(
            updated.count("STATUS:CANCELLED"),
            2,
            "Both master and override VEVENTs must have STATUS:CANCELLED",
        )

    def test_expand_rrule_fast_forwards_old_series_beyond_10k_days(self) -> None:
        """Verify daily recurring series started >10,000 days ago fast-forwards into modern window."""
        old_record = {
            "schema": "calendar-record/1",
            "account": "acc-1",
            "calendar_alias": "cal-1",
            "uid": "daily-since-1990",
            "summary": "Daily Standup Since 1990",
            "all_day": False,
            "start": {
                "utc": "1990-01-01T09:00:00Z",
                "local": "1990-01-01T09:00:00",
                "tzid": "UTC",
                "date": "1990-01-01",
            },
            "end": {
                "utc": "1990-01-01T09:30:00Z",
                "local": "1990-01-01T09:30:00",
                "tzid": "UTC",
                "date": "1990-01-01",
            },
            "duration_minutes": 30,
            "rrule": "FREQ=DAILY",
        }
        occs = expand_record_occurrences(
            old_record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-05T23:59:59Z",
        )
        self.assertEqual(len(occs), 5, "Must return 5 daily occurrences for Nov 1-5, 2026")
        occ_dates = [occ["start"]["date"] for occ in occs]
        self.assertEqual(
            occ_dates,
            ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"],
        )

    def test_expand_rrule_date_only_until_includes_last_day(self) -> None:
        """Verify date-only UNTIL (e.g. UNTIL=20261103) includes occurrences through end of that day."""
        record = {
            "schema": "calendar-record/1",
            "account": "acc-1",
            "calendar_alias": "cal-1",
            "uid": "daily-until-date-only",
            "summary": "Daily with Date-Only UNTIL",
            "all_day": False,
            "start": {
                "utc": "2026-11-01T10:00:00Z",
                "local": "2026-11-01T10:00:00",
                "tzid": "UTC",
                "date": "2026-11-01",
            },
            "end": {
                "utc": "2026-11-01T11:00:00Z",
                "local": "2026-11-01T11:00:00",
                "tzid": "UTC",
                "date": "2026-11-01",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=DAILY;UNTIL=20261103",
        }
        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-30T23:59:59Z",
        )
        occ_dates = [occ["start"]["date"] for occ in occs]
        self.assertEqual(
            occ_dates,
            ["2026-11-01", "2026-11-02", "2026-11-03"],
            "Date-only UNTIL=20261103 must include the 2026-11-03 occurrence",
        )
        self.assertEqual(len(occs), 3)

    def test_expand_rrule_monthly_unordinaled_byday_returns_all_mondays(self) -> None:
        """Verify FREQ=MONTHLY;BYDAY=MO without numeric ordinal expands all Mondays of the month."""
        record = {
            "schema": "calendar-record/1",
            "account": "acc-1",
            "calendar_alias": "cal-1",
            "uid": "monthly-all-mondays",
            "summary": "Every Monday of Month",
            "all_day": False,
            "start": {
                "utc": "2026-11-02T09:00:00Z",
                "local": "2026-11-02T09:00:00",
                "tzid": "UTC",
                "date": "2026-11-02",
            },
            "end": {
                "utc": "2026-11-02T10:00:00Z",
                "local": "2026-11-02T10:00:00",
                "tzid": "UTC",
                "date": "2026-11-02",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=MONTHLY;BYDAY=MO",
        }
        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-30T23:59:59Z",
        )
        occ_dates = [occ["start"]["date"] for occ in occs]
        self.assertEqual(
            occ_dates,
            ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"],
            "FREQ=MONTHLY;BYDAY=MO must expand all 5 Mondays in November 2026",
        )
        self.assertEqual(len(occs), 5)

    def test_expand_rrule_yearly_bymonth_unordinaled_byday(self) -> None:
        """Verify FREQ=YEARLY;BYMONTH=11;BYDAY=MO without numeric ordinal expands all Mondays in November."""
        record = {
            "schema": "calendar-record/1",
            "account": "acc-1",
            "calendar_alias": "cal-1",
            "uid": "yearly-bymonth-all-mondays",
            "summary": "Every Monday in November Yearly",
            "all_day": False,
            "start": {
                "utc": "2026-11-02T09:00:00Z",
                "local": "2026-11-02T09:00:00",
                "tzid": "UTC",
                "date": "2026-11-02",
            },
            "end": {
                "utc": "2026-11-02T10:00:00Z",
                "local": "2026-11-02T10:00:00",
                "tzid": "UTC",
                "date": "2026-11-02",
            },
            "duration_minutes": 60,
            "rrule": "FREQ=YEARLY;BYMONTH=11;BYDAY=MO",
        }
        occs = expand_record_occurrences(
            record,
            window_start_utc="2026-11-01T00:00:00Z",
            window_end_utc="2026-11-30T23:59:59Z",
        )
        occ_dates = [occ["start"]["date"] for occ in occs]
        self.assertEqual(
            occ_dates,
            ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"],
            "FREQ=YEARLY;BYMONTH=11;BYDAY=MO must expand all 5 Mondays in November 2026",
        )
        self.assertEqual(len(occs), 5)

    def test_build_vevent_ics_emits_vtimezone_for_non_utc(self) -> None:
        """Calling build_vevent_ics for non-UTC timezone must include BEGIN:VTIMEZONE,
        TZID:Europe/Vienna, and END:VTIMEZONE before BEGIN:VEVENT."""
        rec = {
            "schema": "calendar-record/1",
            "uid": "tz-test-1",
            "summary": "Vienna Call",
            "start": {
                "local": "2026-11-02T10:00:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
            },
            "end": {
                "local": "2026-11-02T11:00:00",
                "tzid": "Europe/Vienna",
                "all_day": False,
            },
        }
        ics = build_vevent_ics(rec)
        self.assertIn("BEGIN:VTIMEZONE\r\n", ics)
        self.assertIn("TZID:Europe/Vienna\r\n", ics)
        self.assertIn("END:VTIMEZONE\r\n", ics)
        self.assertLess(ics.index("BEGIN:VTIMEZONE"), ics.index("BEGIN:VEVENT"))

        try:
            ics_kw = build_vevent_ics(
                uid="tz-test-1",
                summary="Vienna Call",
                dtstart="2026-11-02T10:00:00",
                dtend="2026-11-02T11:00:00",
                timezone="Europe/Vienna",
            )
            self.assertIn("BEGIN:VTIMEZONE\r\n", ics_kw)
            self.assertIn("TZID:Europe/Vienna\r\n", ics_kw)
            self.assertIn("END:VTIMEZONE\r\n", ics_kw)
            self.assertLess(ics_kw.index("BEGIN:VTIMEZONE"), ics_kw.index("BEGIN:VEVENT"))
        except TypeError:
            pass

    def test_update_vevent_ics_rejects_crlf_and_control_chars(self) -> None:
        """Verify update_vevent_ics rejects CR/LF and control characters across all string update parameters."""
        raw_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//office-intelligence//test//EN\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:evt-crlf-1\r\n"
            "SEQUENCE:1\r\n"
            "DTSTART:20261101T100000Z\r\n"
            "DTEND:20261101T110000Z\r\n"
            "SUMMARY:Standard Event\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        bad_values = ["INJECTED\r\nFOO:BAR", "BAD\nVALUE", "NUL\x00BYTE", "BELL\x07CHAR"]
        fields = ["status", "transp", "classification", "rrule", "dtstart", "dtend", "summary"]

        for f in fields:
            for bad_val in bad_values:
                with self.assertRaises(ICalCodecError, msg=f"Field {f} with value {bad_val!r} must raise ICalCodecError") as ctx:
                    update_vevent_ics(raw_ics, {f: bad_val})
                self.assertIn(
                    ctx.exception.code,
                    {"ICAL_INVALID_PROPERTY", "ICAL_CONTROL_CHAR_FORBIDDEN", "ICAL_INJECTION_DETECTED", "ICAL_CONTROL_CHAR"},
                    f"Expected injection/control char code for field {f}, got {ctx.exception.code}",
                )

    def test_expanded_occurrences_use_iso8601_recurrence_id(self) -> None:
        """Verify expanded occurrences emit canonical ISO-8601 UTC recurrence_id (or YYYY-MM-DD for all-day)."""
        # 1. Timed recurring event
        timed_rec = copy.deepcopy(self.base_record)
        timed_rec["rrule"] = "FREQ=DAILY;COUNT=3"
        timed_occs = expand_record_occurrences(
            timed_rec,
            window_start_utc="2026-11-10T00:00:00Z",
            window_end_utc="2026-11-15T00:00:00Z",
        )
        self.assertGreaterEqual(len(timed_occs), 1)
        for occ in timed_occs:
            rec_id = occ.get("recurrence_id")
            self.assertIsNotNone(rec_id, "Occurrence must have recurrence_id")
            self.assertRegex(
                rec_id,
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                f"Timed occurrence recurrence_id must be ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ), got: {rec_id}",
            )
        self.assertEqual(timed_occs[0]["recurrence_id"], "2026-11-10T09:00:00Z")

        # 2. All-day recurring event
        allday_rec = copy.deepcopy(self.base_record)
        allday_rec["all_day"] = True
        allday_rec["start"] = {
            "all_day": True,
            "date": "2026-11-10",
            "utc": None,
            "local": None,
            "tzid": None,
        }
        allday_rec["end"] = {
            "all_day": True,
            "date": "2026-11-11",
            "utc": None,
            "local": None,
            "tzid": None,
        }
        allday_rec["duration_minutes"] = 1440
        allday_rec["rrule"] = "FREQ=DAILY;COUNT=3"
        allday_occs = expand_record_occurrences(
            allday_rec,
            window_start_utc="2026-11-10T00:00:00Z",
            window_end_utc="2026-11-15T00:00:00Z",
        )
        self.assertGreaterEqual(len(allday_occs), 1)
        for occ in allday_occs:
            rec_id = occ.get("recurrence_id")
            self.assertIsNotNone(rec_id, "All-day occurrence must have recurrence_id")
            self.assertRegex(
                rec_id,
                r"^\d{4}-\d{2}-\d{2}$",
                f"All-day occurrence recurrence_id must be YYYY-MM-DD, got: {rec_id}",
            )
        self.assertEqual(allday_occs[0]["recurrence_id"], "2026-11-10")


if __name__ == "__main__":
    unittest.main()
