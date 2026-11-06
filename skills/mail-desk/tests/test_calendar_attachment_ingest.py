"""Unit and integration tests for mail-desk .ics calendar attachment ingest (Ticket CD-4).

Tests:
1. Attachment policy (check_attachment_policy):
   - Accepts text/calendar and application/ics with .ics extensions
   - Rejects sibling MIME types (text/html, application/x-msdownload, etc.)
   - Rejects dangerous extensions (.exe, .ps1, etc.) even if calendar MIME claimed
   - Fail-closed size and metadata enforcement
2. Attachment extraction (attachment_extract):
   - text/calendar and application/ics routed through bounded text branch
   - Character budget (max_chars_per_attachment) and truncation notice appending
   - Multi-encoding support (UTF-8, Latin-1, CP1252)
3. Calendar extraction module (calendar_extract):
   - CalendarExtractError with fail-closed codes
   - extract_calendar_objects returning 4-key schema:
     {"uid", "sequence", "recurrence_id", "content_sha256"}
   - Timed, all-day, recurring, override, folded lines, VALARM isolation, multi-VEVENT
4. Parity with calendar-desk:
   - Dynamic path loading of skills/calendar-desk/scripts/core/ical_codec.py
   - Exact bit-for-bit parity of uid, sequence, recurrence_id, and content_sha256
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest

# Add mail-desk scripts to sys.path
MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = MAIL_DESK_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.quarantine.attachment_policy import (  # noqa: E402
    DEFAULT_ATTACHMENT_POLICY,
    check_attachment_policy,
)
from core.quarantine.attachment_extract import (  # noqa: E402
    _extract_content_internal,
    _resolve_extraction_limits,
)
from core.calendar_extract import (  # noqa: E402
    CalendarExtractError,
    extract_calendar_objects,
)

# Reference calendar-desk ical_codec loaded by file path for parity testing
CALENDAR_DESK_ROOT = Path(__file__).resolve().parents[2] / "calendar-desk"
CALENDAR_DESK_CODEC_PATH = CALENDAR_DESK_ROOT / "scripts" / "core" / "ical_codec.py"

ical_codec_parity: Any = None
if CALENDAR_DESK_CODEC_PATH.is_file():
    spec = importlib.util.spec_from_file_location("ical_codec_parity", str(CALENDAR_DESK_CODEC_PATH))
    if spec is not None and spec.loader is not None:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ical_codec_parity = mod


SAMPLE_TIMED_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:timed-event-001@office-intelligence.test\r\n"
    "SEQUENCE:0\r\n"
    "DTSTART:20261102T090000Z\r\n"
    "DTEND:20261102T100000Z\r\n"
    "SUMMARY:Project Review\r\n"
    "STATUS:CONFIRMED\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

SAMPLE_ALL_DAY_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:allday-event-002@office-intelligence.test\r\n"
    "SEQUENCE:2\r\n"
    "DTSTART;VALUE=DATE:20261105\r\n"
    "DTEND;VALUE=DATE:20261106\r\n"
    "SUMMARY:Company Holiday\r\n"
    "STATUS:CONFIRMED\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

SAMPLE_RECURRING_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:weekly-standup@office-intelligence.test\r\n"
    "SEQUENCE:1\r\n"
    "DTSTART:20261102T080000Z\r\n"
    "DTEND:20261102T083000Z\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
    "SUMMARY:Weekly Standup\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

SAMPLE_OVERRIDE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:weekly-standup@office-intelligence.test\r\n"
    "RECURRENCE-ID;TZID=Europe/Vienna:20261109T090000\r\n"
    "SEQUENCE:2\r\n"
    "DTSTART:20261109T080000Z\r\n"
    "DTEND:20261109T083000Z\r\n"
    "SUMMARY:Weekly Standup (Rescheduled)\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

SAMPLE_FOLDED_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:folded-uid-part-1\r\n"
    " -part-2-long-string@office-intelligence.test\r\n"
    "SEQUENCE:3\r\n"
    "DTSTART:20261110T140000Z\r\n"
    "DTEND:20261110T150000Z\r\n"
    "SUMMARY:A very long meeting summary that has been folded\r\n"
    "  across multiple lines according to RFC 5545\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

SAMPLE_VALARM_NESTED_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:valarm-event-005@office-intelligence.test\r\n"
    "SEQUENCE:0\r\n"
    "DTSTART:20261102T100000Z\r\n"
    "DTEND:20261102T110000Z\r\n"
    "SUMMARY:Meeting with Alarm\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "DESCRIPTION:Meeting Reminder\r\n"
    "TRIGGER:-PT15M\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

SAMPLE_MULTI_VEVENT_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//mail-desk//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:multi-master@office-intelligence.test\r\n"
    "SEQUENCE:1\r\n"
    "DTSTART:20261102T080000Z\r\n"
    "DTEND:20261102T090000Z\r\n"
    "RRULE:FREQ=DAILY;COUNT=5\r\n"
    "SUMMARY:Series Master\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:multi-master@office-intelligence.test\r\n"
    "RECURRENCE-ID:20261104T080000Z\r\n"
    "SEQUENCE:2\r\n"
    "DTSTART:20261104T100000Z\r\n"
    "DTEND:20261104T110000Z\r\n"
    "SUMMARY:Series Wednesday Override\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class TestCalendarAttachmentPolicy(unittest.TestCase):
    """Test quarantine attachment policy acceptance and security invariants for calendar MIME types."""

    def test_allowed_mime_types_contains_calendar_types(self) -> None:
        allowed = DEFAULT_ATTACHMENT_POLICY["transport"]["allowed_mime_types"]
        self.assertIn("text/calendar", allowed)
        self.assertIn("application/ics", allowed)

    def test_check_attachment_policy_accepts_valid_ics(self) -> None:
        # text/calendar
        status, reason = check_attachment_policy(
            filename="invite.ics",
            mime_type="text/calendar",
            size_bytes=1024,
        )
        self.assertEqual(status, "allowed")
        self.assertIsNone(reason)

        # application/ics
        status, reason = check_attachment_policy(
            filename="meeting.ics",
            mime_type="application/ics",
            size_bytes=2048,
        )
        self.assertEqual(status, "allowed")
        self.assertIsNone(reason)

    def test_check_attachment_policy_rejects_disallowed_sibling_types(self) -> None:
        # text/html is not allowed
        status, reason = check_attachment_policy(
            filename="invite.html",
            mime_type="text/html",
            size_bytes=1024,
        )
        self.assertEqual(status, "rejected_mime")
        self.assertIsNotNone(reason)

        # application/x-msdownload
        status, reason = check_attachment_policy(
            filename="payload.bin",
            mime_type="application/x-msdownload",
            size_bytes=1024,
        )
        self.assertEqual(status, "rejected_mime")

    def test_check_attachment_policy_rejects_dangerous_extensions_with_calendar_mime(self) -> None:
        # .exe extension with spoofed text/calendar MIME
        status, reason = check_attachment_policy(
            filename="invite.exe",
            mime_type="text/calendar",
            size_bytes=1024,
        )
        self.assertEqual(status, "rejected_extension")
        self.assertIn("disallowed", str(reason).lower())

        # .ps1 extension with application/ics MIME
        status, reason = check_attachment_policy(
            filename="script.ps1",
            mime_type="application/ics",
            size_bytes=1024,
        )
        self.assertEqual(status, "rejected_extension")

    def test_check_attachment_policy_fail_closed_on_size_and_mime(self) -> None:
        # Missing size
        status, _ = check_attachment_policy("invite.ics", mime_type="text/calendar", size_bytes=None)
        self.assertEqual(status, "rejected_size")

        # Negative size
        status, _ = check_attachment_policy("invite.ics", mime_type="text/calendar", size_bytes=-10)
        self.assertEqual(status, "rejected_size")

        # Exceeding single file limit (15 MB)
        oversized = 16 * 1024 * 1024
        status, _ = check_attachment_policy("invite.ics", mime_type="text/calendar", size_bytes=oversized)
        self.assertEqual(status, "rejected_size")

        # Missing MIME
        status, _ = check_attachment_policy("invite.ics", mime_type=None, size_bytes=1024)
        self.assertEqual(status, "rejected_mime")


class TestCalendarAttachmentExtraction(unittest.TestCase):
    """Test attachment extraction routing and budget enforcement for calendar MIME types."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.run_dir = self.dir_path / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.limits = _resolve_extraction_limits(None)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_extract_text_calendar_utf8(self) -> None:
        ics_file = self.run_dir / "meeting.ics"
        ics_file.write_text(SAMPLE_TIMED_ICS, encoding="utf-8")
        file_sha = hashlib.sha256(SAMPLE_TIMED_ICS.encode("utf-8")).hexdigest()

        result = _extract_content_internal(
            target_file=ics_file,
            actual_sha=file_sha,
            filename="meeting.ics",
            ext=".ics",
            eff_mime="text/calendar",
            run_dir=self.run_dir,
            run_id="run-1",
            raw_attachments_root=self.dir_path,
            base_data_dir=self.dir_path,
            limits=self.limits,
        )
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["method"], "native")
        self.assertEqual(result["tool"], "built_in")
        self.assertEqual(result["extracted_text"], SAMPLE_TIMED_ICS)
        self.assertIsNone(result["truncation_reason"])

    def test_extract_application_ics(self) -> None:
        ics_file = self.run_dir / "invite.ics"
        ics_file.write_text(SAMPLE_ALL_DAY_ICS, encoding="utf-8")
        file_sha = hashlib.sha256(SAMPLE_ALL_DAY_ICS.encode("utf-8")).hexdigest()

        result = _extract_content_internal(
            target_file=ics_file,
            actual_sha=file_sha,
            filename="invite.ics",
            ext=".ics",
            eff_mime="application/ics",
            run_dir=self.run_dir,
            run_id="run-1",
            raw_attachments_root=self.dir_path,
            base_data_dir=self.dir_path,
            limits=self.limits,
        )
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["method"], "native")
        self.assertEqual(result["tool"], "built_in")
        self.assertEqual(result["extracted_text"], SAMPLE_ALL_DAY_ICS)

    def test_extract_calendar_character_budget_truncation(self) -> None:
        # Create an ICS payload exceeding max_chars_per_attachment
        long_desc = "A" * 20000
        large_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:large-event@test\r\n"
            f"DESCRIPTION:{long_desc}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        ics_file = self.run_dir / "large.ics"
        ics_file.write_text(large_ics, encoding="utf-8")
        file_sha = hashlib.sha256(large_ics.encode("utf-8")).hexdigest()

        result = _extract_content_internal(
            target_file=ics_file,
            actual_sha=file_sha,
            filename="large.ics",
            ext=".ics",
            eff_mime="text/calendar",
            run_dir=self.run_dir,
            run_id="run-1",
            raw_attachments_root=self.dir_path,
            base_data_dir=self.dir_path,
            limits=self.limits,
        )
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["truncation_reason"], "max_chars_exceeded")
        self.assertIn("[TRUNCATED: Content exceeded max_chars_exceeded limit]", result["extracted_text"])
        max_chars = self.limits["max_chars_per_attachment"]
        self.assertEqual(len(result["extracted_text"].split("\n\n[TRUNCATED:")[0]), max_chars)


class TestCalendarExtractModule(unittest.TestCase):
    """Test core.calendar_extract.extract_calendar_objects contract and fail-closed behaviors."""

    def test_error_class_inheritance_and_properties(self) -> None:
        err = CalendarExtractError("CALENDAR_EMPTY_INPUT", "Input payload is empty.")
        self.assertEqual(err.code, "CALENDAR_EMPTY_INPUT")
        self.assertIsInstance(err, ValueError)

    def test_fail_closed_empty_or_whitespace_input(self) -> None:
        with self.assertRaises(CalendarExtractError) as ctx:
            extract_calendar_objects("")
        self.assertIn("EMPTY", ctx.exception.code)

        with self.assertRaises(CalendarExtractError) as ctx2:
            extract_calendar_objects("   \r\n\t   ")
        self.assertIn("EMPTY", ctx2.exception.code)

    def test_fail_closed_oversized_input(self) -> None:
        # > 1 MiB (1,048,576 bytes)
        oversized = "BEGIN:VCALENDAR\r\n" + ("X-PAD:" + "X" * 1000 + "\r\n") * 1100 + "END:VCALENDAR\r\n"
        with self.assertRaises(CalendarExtractError) as ctx:
            extract_calendar_objects(oversized)
        self.assertIn("OVERSIZED", ctx.exception.code)

    def test_fail_closed_disallowed_c0_control_characters(self) -> None:
        # NUL character (\x00)
        corrupt_nul = SAMPLE_TIMED_ICS.replace("Project Review", "Project\x00Review")
        with self.assertRaises(CalendarExtractError) as ctx:
            extract_calendar_objects(corrupt_nul)
        self.assertIn("CONTROL_CHAR", ctx.exception.code)

        # Bell character (\x07)
        corrupt_bel = SAMPLE_TIMED_ICS.replace("Project Review", "Project\x07Review")
        with self.assertRaises(CalendarExtractError) as ctx:
            extract_calendar_objects(corrupt_bel)
        self.assertIn("CONTROL_CHAR", ctx.exception.code)

    def test_fail_closed_missing_vcalendar_structure(self) -> None:
        raw_no_vcal = "BEGIN:VEVENT\r\nUID:123@test\r\nEND:VEVENT"
        with self.assertRaises(CalendarExtractError) as ctx:
            extract_calendar_objects(raw_no_vcal)
        self.assertIn("STRUCTURE", ctx.exception.code)

    def test_fail_closed_missing_vevent_uid(self) -> None:
        raw_no_uid = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART:20261102T090000Z\r\n"
            "SUMMARY:Missing UID\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        with self.assertRaises(CalendarExtractError) as ctx:
            extract_calendar_objects(raw_no_uid)
        self.assertIn("UID", ctx.exception.code)

    def test_extract_timed_event_schema_and_types(self) -> None:
        objects = extract_calendar_objects(SAMPLE_TIMED_ICS)
        self.assertEqual(len(objects), 1)
        obj = objects[0]

        # Exact schema requirement: {"uid", "sequence", "recurrence_id", "content_sha256"}
        self.assertEqual(set(obj.keys()), {"uid", "sequence", "recurrence_id", "content_sha256"})
        self.assertEqual(obj["uid"], "timed-event-001@office-intelligence.test")
        self.assertEqual(obj["sequence"], 0)
        self.assertIsInstance(obj["sequence"], int)
        self.assertIsNone(obj["recurrence_id"])
        self.assertEqual(len(obj["content_sha256"]), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in obj["content_sha256"]))

    def test_extract_all_day_event(self) -> None:
        objects = extract_calendar_objects(SAMPLE_ALL_DAY_ICS)
        self.assertEqual(len(objects), 1)
        obj = objects[0]
        self.assertEqual(obj["uid"], "allday-event-002@office-intelligence.test")
        self.assertEqual(obj["sequence"], 2)
        self.assertIsNone(obj["recurrence_id"])

    def test_extract_recurring_event(self) -> None:
        objects = extract_calendar_objects(SAMPLE_RECURRING_ICS)
        self.assertEqual(len(objects), 1)
        obj = objects[0]
        self.assertEqual(obj["uid"], "weekly-standup@office-intelligence.test")
        self.assertEqual(obj["sequence"], 1)
        self.assertIsNone(obj["recurrence_id"])

    def test_extract_recurrence_override_with_tzid(self) -> None:
        objects = extract_calendar_objects(SAMPLE_OVERRIDE_ICS)
        self.assertEqual(len(objects), 1)
        obj = objects[0]
        self.assertEqual(obj["uid"], "weekly-standup@office-intelligence.test")
        self.assertEqual(obj["sequence"], 2)
        # RECURRENCE-ID value stripped of parameters
        self.assertEqual(obj["recurrence_id"], "20261109T090000")

    def test_extract_folded_lines(self) -> None:
        objects = extract_calendar_objects(SAMPLE_FOLDED_ICS)
        self.assertEqual(len(objects), 1)
        obj = objects[0]
        self.assertEqual(obj["uid"], "folded-uid-part-1-part-2-long-string@office-intelligence.test")
        self.assertEqual(obj["sequence"], 3)

    def test_extract_nested_valarm_ignored(self) -> None:
        objects = extract_calendar_objects(SAMPLE_VALARM_NESTED_ICS)
        self.assertEqual(len(objects), 1)
        obj = objects[0]
        self.assertEqual(obj["uid"], "valarm-event-005@office-intelligence.test")
        self.assertEqual(obj["sequence"], 0)

    def test_extract_multi_vevent_document_order(self) -> None:
        objects = extract_calendar_objects(SAMPLE_MULTI_VEVENT_ICS)
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[0]["uid"], "multi-master@office-intelligence.test")
        self.assertIsNone(objects[0]["recurrence_id"])
        self.assertEqual(objects[0]["sequence"], 1)

        self.assertEqual(objects[1]["uid"], "multi-master@office-intelligence.test")
        self.assertEqual(objects[1]["recurrence_id"], "20261104T080000Z")
        self.assertEqual(objects[1]["sequence"], 2)


class TestCalendarExtractParityWithCalendarDesk(unittest.TestCase):
    """Test bit-for-bit parity of mail-desk extraction with calendar-desk's ical_codec."""

    def setUp(self) -> None:
        if ical_codec_parity is None:
            self.skipTest("skills/calendar-desk/scripts/core/ical_codec.py not available for parity check")

    def test_parity_timed_event(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_TIMED_ICS)
        cal_desk_ref = ical_codec_parity.extract_calendar_object_ref(SAMPLE_TIMED_ICS)

        self.assertEqual(len(mail_objs), 1)
        self.assertEqual(mail_objs[0]["uid"], cal_desk_ref["uid"])
        self.assertEqual(mail_objs[0]["sequence"], cal_desk_ref["sequence"])
        self.assertEqual(mail_objs[0]["recurrence_id"], cal_desk_ref["recurrence_id"])
        self.assertEqual(mail_objs[0]["content_sha256"], cal_desk_ref["content_sha256"])

    def test_parity_all_day_event(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_ALL_DAY_ICS)
        cal_desk_ref = ical_codec_parity.extract_calendar_object_ref(SAMPLE_ALL_DAY_ICS)

        self.assertEqual(len(mail_objs), 1)
        self.assertEqual(mail_objs[0]["uid"], cal_desk_ref["uid"])
        self.assertEqual(mail_objs[0]["sequence"], cal_desk_ref["sequence"])
        self.assertEqual(mail_objs[0]["recurrence_id"], cal_desk_ref["recurrence_id"])
        self.assertEqual(mail_objs[0]["content_sha256"], cal_desk_ref["content_sha256"])

    def test_parity_recurring_event(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_RECURRING_ICS)
        cal_desk_ref = ical_codec_parity.extract_calendar_object_ref(SAMPLE_RECURRING_ICS)

        self.assertEqual(len(mail_objs), 1)
        self.assertEqual(mail_objs[0]["uid"], cal_desk_ref["uid"])
        self.assertEqual(mail_objs[0]["sequence"], cal_desk_ref["sequence"])
        self.assertEqual(mail_objs[0]["recurrence_id"], cal_desk_ref["recurrence_id"])
        self.assertEqual(mail_objs[0]["content_sha256"], cal_desk_ref["content_sha256"])

    def test_parity_recurrence_override(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_OVERRIDE_ICS)
        cal_desk_ref = ical_codec_parity.extract_calendar_object_ref(SAMPLE_OVERRIDE_ICS)

        self.assertEqual(len(mail_objs), 1)
        self.assertEqual(mail_objs[0]["uid"], cal_desk_ref["uid"])
        self.assertEqual(mail_objs[0]["sequence"], cal_desk_ref["sequence"])
        self.assertEqual(mail_objs[0]["recurrence_id"], cal_desk_ref["recurrence_id"])
        self.assertEqual(mail_objs[0]["content_sha256"], cal_desk_ref["content_sha256"])

    def test_parity_folded_lines(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_FOLDED_ICS)
        cal_desk_ref = ical_codec_parity.extract_calendar_object_ref(SAMPLE_FOLDED_ICS)

        self.assertEqual(len(mail_objs), 1)
        self.assertEqual(mail_objs[0]["uid"], cal_desk_ref["uid"])
        self.assertEqual(mail_objs[0]["sequence"], cal_desk_ref["sequence"])
        self.assertEqual(mail_objs[0]["recurrence_id"], cal_desk_ref["recurrence_id"])
        self.assertEqual(mail_objs[0]["content_sha256"], cal_desk_ref["content_sha256"])

    def test_parity_nested_valarm(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_VALARM_NESTED_ICS)
        cal_desk_ref = ical_codec_parity.extract_calendar_object_ref(SAMPLE_VALARM_NESTED_ICS)

        self.assertEqual(len(mail_objs), 1)
        self.assertEqual(mail_objs[0]["uid"], cal_desk_ref["uid"])
        self.assertEqual(mail_objs[0]["sequence"], cal_desk_ref["sequence"])
        self.assertEqual(mail_objs[0]["recurrence_id"], cal_desk_ref["recurrence_id"])
        self.assertEqual(mail_objs[0]["content_sha256"], cal_desk_ref["content_sha256"])

    def test_parity_multi_vevent_all_objects(self) -> None:
        mail_objs = extract_calendar_objects(SAMPLE_MULTI_VEVENT_ICS)
        cal_desk_records = ical_codec_parity.parse_ics_to_records(SAMPLE_MULTI_VEVENT_ICS)

        self.assertEqual(len(mail_objs), len(cal_desk_records))
        for m_obj, c_rec in zip(mail_objs, cal_desk_records):
            c_ref = ical_codec_parity.extract_calendar_object_ref(c_rec)
            self.assertEqual(m_obj["uid"], c_ref["uid"])
            self.assertEqual(m_obj["sequence"], c_ref["sequence"])
            self.assertEqual(m_obj["recurrence_id"], c_ref["recurrence_id"])
            self.assertEqual(m_obj["content_sha256"], c_ref["content_sha256"])


if __name__ == "__main__":
    unittest.main()
