"""Unit tests for Free/Busy computation, slot finding engine, and CLI entry point.

Tests skills/calendar-desk/scripts/core/freebusy.py and
skills/calendar-desk/scripts/calendar_desk_client.py contracts:
- compute_freebusy: OPAQUE vs TRANSPARENT filtering, CANCELLED exclusion, interval merging, conflict detection
- find_free_slots: working hours in IANA timezone, buffer padding, min slot duration, weekdays-only filtering, termino-api format ("YYYY-MM-DD|HH:MM->HH:MM")
- calendar_desk_client CLI entry point: --json envelope, freebusy, find-slots subcommands
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

# Add calendar-desk scripts to sys.path
CALENDAR_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALENDAR_DESK_ROOT / "scripts"))

from core.freebusy import compute_freebusy, find_free_slots  # noqa: E402
import calendar_desk_client  # noqa: E402


class TestFreeBusyEngine(unittest.TestCase):
    def test_compute_freebusy_empty_records(self) -> None:
        res = compute_freebusy(
            [],
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-03T00:00:00Z",
        )
        self.assertEqual(res["busy_intervals"], [])
        self.assertEqual(res["merged_busy"], [])
        self.assertEqual(res["conflicts"], [])
        self.assertEqual(res["total_busy_minutes"], 0)

    def test_compute_freebusy_zero_duration_ignored(self) -> None:
        records = [
            {
                "schema": "calendar-record/1",
                "uid": "event-zero@test",
                "summary": "Zero Duration Milestone",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 0,
            }
        ]
        res = compute_freebusy(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-03T00:00:00Z",
        )
        self.assertEqual(res["busy_intervals"], [])
        self.assertEqual(res["merged_busy"], [])
        self.assertEqual(res["total_busy_minutes"], 0)

    def test_compute_freebusy_opaque_vs_transparent(self) -> None:
        records = [
            # Opaque event -> busy
            {
                "schema": "calendar-record/1",
                "uid": "event-opaque@test",
                "summary": "Focus Time",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T09:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
            # Transparent event -> NOT busy
            {
                "schema": "calendar-record/1",
                "uid": "event-transp@test",
                "summary": "Reminder (Free)",
                "status": "CONFIRMED",
                "transp": "TRANSPARENT",
                "start": {"utc": "2026-11-02T10:30:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T11:30:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
        ]
        res = compute_freebusy(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-03T00:00:00Z",
        )
        self.assertEqual(len(res["busy_intervals"]), 1)
        self.assertEqual(res["busy_intervals"][0]["uid"], "event-opaque@test")
        self.assertEqual(res["total_busy_minutes"], 60)

    def test_compute_freebusy_cancelled_ignored(self) -> None:
        records = [
            {
                "schema": "calendar-record/1",
                "uid": "event-confirmed@test",
                "summary": "Project Sync",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T09:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
            {
                "schema": "calendar-record/1",
                "uid": "event-cancelled@test",
                "summary": "Cancelled Workshop",
                "status": "CANCELLED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T11:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T12:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
        ]
        res = compute_freebusy(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-03T00:00:00Z",
        )
        self.assertEqual(len(res["busy_intervals"]), 1)
        self.assertEqual(res["busy_intervals"][0]["uid"], "event-confirmed@test")
        self.assertEqual(res["total_busy_minutes"], 60)

    def test_compute_freebusy_overlapping_merged_and_conflicts_detected(self) -> None:
        records = [
            # Event 1: 09:00 - 10:30 UTC
            {
                "schema": "calendar-record/1",
                "uid": "event-1@test",
                "summary": "Sprint Planning",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T09:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T10:30:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 90,
            },
            # Event 2: 10:00 - 11:00 UTC (overlaps Event 1 between 10:00 and 10:30)
            {
                "schema": "calendar-record/1",
                "uid": "event-2@test",
                "summary": "Client Call",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T11:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
            # Event 3: 13:00 - 14:00 UTC (separate interval)
            {
                "schema": "calendar-record/1",
                "uid": "event-3@test",
                "summary": "1:1 Meeting",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T13:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T14:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
        ]
        res = compute_freebusy(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-03T00:00:00Z",
        )
        self.assertEqual(len(res["busy_intervals"]), 3)
        # Merged busy intervals: [09:00-11:00] (120 mins) and [13:00-14:00] (60 mins)
        self.assertEqual(len(res["merged_busy"]), 2)
        self.assertEqual(res["merged_busy"][0]["start_utc"], "2026-11-02T09:00:00Z")
        self.assertEqual(res["merged_busy"][0]["end_utc"], "2026-11-02T11:00:00Z")
        self.assertEqual(res["merged_busy"][0]["duration_minutes"], 120)
        self.assertEqual(res["merged_busy"][1]["start_utc"], "2026-11-02T13:00:00Z")
        self.assertEqual(res["merged_busy"][1]["end_utc"], "2026-11-02T14:00:00Z")
        self.assertEqual(res["total_busy_minutes"], 180)

        # Conflict detection
        self.assertEqual(len(res["conflicts"]), 1)
        conflict = res["conflicts"][0]
        self.assertIn(conflict["event_a"], ["event-1@test", "event-2@test"])
        self.assertIn(conflict["event_b"], ["event-1@test", "event-2@test"])
        self.assertEqual(conflict["overlap_start_utc"], "2026-11-02T10:00:00Z")
        self.assertEqual(conflict["overlap_end_utc"], "2026-11-02T10:30:00Z")

    def test_compute_freebusy_adjacent_intervals_merged(self) -> None:
        records = [
            # 09:00 - 10:00 UTC
            {
                "schema": "calendar-record/1",
                "uid": "event-adj1@test",
                "summary": "Part 1",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T09:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
            # 10:00 - 11:00 UTC (directly adjacent)
            {
                "schema": "calendar-record/1",
                "uid": "event-adj2@test",
                "summary": "Part 2",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T10:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T11:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            },
        ]
        res = compute_freebusy(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-03T00:00:00Z",
        )
        self.assertEqual(len(res["merged_busy"]), 1)
        self.assertEqual(res["merged_busy"][0]["start_utc"], "2026-11-02T09:00:00Z")
        self.assertEqual(res["merged_busy"][0]["end_utc"], "2026-11-02T11:00:00Z")
        self.assertEqual(res["merged_busy"][0]["duration_minutes"], 120)
        # Adjacent is not a conflict
        self.assertEqual(len(res["conflicts"]), 0)


class TestFindFreeSlots(unittest.TestCase):
    def test_find_free_slots_single_day_with_busy(self) -> None:
        # Monday 2026-11-02 in Europe/Vienna (CET = UTC+1)
        # Working hours 09:00 - 17:00 local (08:00 - 16:00 UTC)
        # Busy from 12:00 to 13:00 local (11:00 to 12:00 UTC)
        records = [
            {
                "schema": "calendar-record/1",
                "uid": "lunch@test",
                "summary": "Lunch Break",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {
                    "utc": "2026-11-02T11:00:00Z",
                    "local": "2026-11-02T12:00:00+01:00",
                    "date": "2026-11-02",
                    "tzid": "Europe/Vienna",
                    "all_day": False,
                },
                "end": {
                    "utc": "2026-11-02T12:00:00Z",
                    "local": "2026-11-02T13:00:00+01:00",
                    "date": "2026-11-02",
                    "tzid": "Europe/Vienna",
                    "all_day": False,
                },
                "duration_minutes": 60,
            }
        ]
        res = find_free_slots(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-02T23:59:59Z",
            tzid="Europe/Vienna",
            working_hours="09:00-17:00",
            min_slot_minutes=60,
            buffer_minutes=0,
            weekdays_only=True,
        )
        self.assertIn("slots", res)
        self.assertIn("termino_slots", res)
        self.assertEqual(len(res["slots"]), 2)

        # Slot 1: 09:00 - 12:00 local
        self.assertEqual(res["slots"][0]["start_local"], "2026-11-02T09:00:00+01:00")
        self.assertEqual(res["slots"][0]["end_local"], "2026-11-02T12:00:00+01:00")
        self.assertEqual(res["slots"][0]["duration_minutes"], 180)

        # Slot 2: 13:00 - 17:00 local
        self.assertEqual(res["slots"][1]["start_local"], "2026-11-02T13:00:00+01:00")
        self.assertEqual(res["slots"][1]["end_local"], "2026-11-02T17:00:00+01:00")
        self.assertEqual(res["slots"][1]["duration_minutes"], 240)

        # Check termino-api formatting: "YYYY-MM-DD|HH:MM->HH:MM"
        self.assertEqual(
            res["termino_slots"],
            ["2026-11-02|09:00->12:00", "2026-11-02|13:00->17:00"],
        )

    def test_find_free_slots_with_buffer_minutes(self) -> None:
        # Same setup with 15 mins buffer
        # Busy 12:00-13:00 local -> padded busy 11:45-13:15 local
        # Free: 09:00-11:45 (165m), 13:15-17:00 (225m)
        records = [
            {
                "schema": "calendar-record/1",
                "uid": "busy-1@test",
                "summary": "Review",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T11:00:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T12:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 60,
            }
        ]
        res = find_free_slots(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-02T23:59:59Z",
            tzid="Europe/Vienna",
            working_hours="09:00-17:00",
            min_slot_minutes=60,
            buffer_minutes=15,
            weekdays_only=True,
        )
        self.assertEqual(len(res["slots"]), 2)
        self.assertEqual(res["slots"][0]["duration_minutes"], 165)
        self.assertEqual(res["slots"][1]["duration_minutes"], 225)
        self.assertEqual(
            res["termino_slots"],
            ["2026-11-02|09:00->11:45", "2026-11-02|13:15->17:00"],
        )

    def test_find_free_slots_min_slot_filter(self) -> None:
        # Working hours 09:00-17:00. Busy 09:45-17:00.
        # First slot is 09:00-09:45 (45 mins). With min_slot_minutes=60, it must be excluded!
        records = [
            {
                "schema": "calendar-record/1",
                "uid": "almost-full@test",
                "summary": "Long workshop",
                "status": "CONFIRMED",
                "transp": "OPAQUE",
                "start": {"utc": "2026-11-02T08:45:00Z", "date": "2026-11-02", "all_day": False},
                "end": {"utc": "2026-11-02T16:00:00Z", "date": "2026-11-02", "all_day": False},
                "duration_minutes": 435,
            }
        ]
        res = find_free_slots(
            records,
            window_start_utc="2026-11-02T00:00:00Z",
            window_end_utc="2026-11-02T23:59:59Z",
            tzid="Europe/Vienna",
            working_hours="09:00-17:00",
            min_slot_minutes=60,
        )
        self.assertEqual(res["slots"], [])
        self.assertEqual(res["termino_slots"], [])

    def test_find_free_slots_weekdays_only_skips_weekends(self) -> None:
        # Window spans Friday 2026-11-06 to Monday 2026-11-09
        # Saturday Nov 7 and Sunday Nov 8 must have 0 slots
        res = find_free_slots(
            [],
            window_start_utc="2026-11-06T00:00:00Z",
            window_end_utc="2026-11-09T23:59:59Z",
            tzid="Europe/Vienna",
            working_hours="09:00-17:00",
            min_slot_minutes=60,
            weekdays_only=True,
        )
        dates = [slot["date"] for slot in res["slots"]]
        self.assertIn("2026-11-06", dates)  # Friday
        self.assertNotIn("2026-11-07", dates)  # Saturday
        self.assertNotIn("2026-11-08", dates)  # Sunday
        self.assertIn("2026-11-09", dates)  # Monday


class TestCalendarDeskClientCLI(unittest.TestCase):
    def test_cli_help(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = calendar_desk_client.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("calendar-desk", out.getvalue().lower())

    def test_cli_freebusy_empty_records_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = calendar_desk_client.main([
                "--json",
                "freebusy",
                "--window-start", "2026-11-02T00:00:00Z",
                "--window-end", "2026-11-03T00:00:00Z",
            ])
        self.assertEqual(code, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed.get("status"), "ok")
        self.assertEqual(parsed.get("action"), "freebusy")
        self.assertIn("busy_intervals", parsed.get("data", {}))

    def test_cli_find_slots_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = calendar_desk_client.main([
                "--json",
                "find-slots",
                "--window-start", "2026-11-02T00:00:00Z",
                "--window-end", "2026-11-02T23:59:59Z",
                "--termino-slots",
            ])
        self.assertEqual(code, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed.get("status"), "ok")
        self.assertEqual(parsed.get("action"), "find-slots")
        self.assertIn("termino_slots", parsed.get("data", {}))

    def test_cli_emit_result_agenda_and_search_includes_fenced_records(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            calendar_desk_client._emit_result(
                "agenda",
                {
                    "records": [{"summary": "Test"}],
                    "fenced_records": [{"summary": "<untrusted_calendar_content>Test</untrusted_calendar_content>"}],
                    "item_count": 1,
                },
                as_json=True,
            )
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed.get("action"), "agenda")
        self.assertIn("fenced_records", parsed.get("data", {}))
        self.assertEqual(len(parsed["data"]["fenced_records"]), 1)


if __name__ == "__main__":
    unittest.main()
