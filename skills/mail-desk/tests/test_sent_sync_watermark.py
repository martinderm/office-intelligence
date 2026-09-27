"""FR-26/MD-SE1+SE2 sent-sync watermark & fail-closed contract tests.

Hermetic sent-indexer contract. Exercises the real sync path against a
temporarily materialized sent-index and a mocked Himalaya; no mailbox,
network or catalog writes outside the temp dir.

Red-Gate history: at the MD-SE1-001 dispatch the no-dates sync ignored
``count`` (hardwired ``range(7)``), derived no window from the index
watermark, swallowed per-date errors via ``except Exception: return 0``
(ok:true despite Himalaya failures) and reported the misleading
``total_envelopes_examined``; the tests below failed then and pin the
watermark-based, fail-closed, telemetry-honest contract since.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import sent_indexer  # noqa: E402
from core.modes import sync_sent as sync_sent_mode  # noqa: E402


def _index_entry(at: str, mid: str, eid: str) -> dict:
    return {
        "schema_version": 1,
        "at": at,
        "updated_at": at,
        "mailbox": "ACC",
        "message_id": f"<{mid}>",
        "in_reply_to": "",
        "references": [],
        "subject": "sent",
        "from": "me@example.test",
        "to": ["x@example.test"],
        "folder": "Sent Items",
        "sent_envelope_id": eid,
        "backend_locator": f"{eid}|{mid}",
        "note": "Gesendet",
    }


def _himalaya_json(envelopes: list[dict]) -> str:
    return json.dumps(envelopes, ensure_ascii=False)


class WatermarkWindowTests(unittest.TestCase):
    """No-dates sync derives its window from the index watermark, not range(7)."""

    def test_window_covers_watermark_gap(self) -> None:
        # Index watermark 2026-09-11; today 2026-09-27 -> the gap days
        # 2026-09-12..2026-09-19 MUST be queried (Env 2026-W40/1 finding).
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)
            entry = _index_entry("2026-09-11T10:00:00Z", "old@x", "9001")
            (dd / "sent-index.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

            queried_dates: list[str] = []

            def fake_run(args, account=None, timeout=30, max_retries=5):
                for a in args:
                    if a.startswith("date "):
                        queried_dates.append(a[len("date "):])
                return "[]"

            from datetime import datetime as real_datetime

            class _FrozenDatetime(real_datetime):
                @classmethod
                def now(cls):
                    return real_datetime(2026, 9, 27, 12, 0, 0)

            with patch.object(sent_indexer, "run_himalaya", side_effect=fake_run), \
                 patch.object(sent_indexer, "datetime", _FrozenDatetime):
                sent_indexer.sync_sent_items(count=150, account="ACC", data_dir=dd)

            self.assertIn("2026-09-12", queried_dates, "watermark gap days must be queried (FR-26/MD-SE1)")
            self.assertIn("2026-09-19", queried_dates, "gap up to the old 7-day window must be covered")
            self.assertIn("2026-09-27", queried_dates, "today must be covered")

    def test_count_respected_when_larger_than_gap(self) -> None:
        # A recent watermark with count=3 must yield a bounded, deterministic
        # window: count semantics must not be dead input.  Time is frozen (like
        # test 1) so the expectation cannot drift with the real "today".
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)
            entry = _index_entry("2026-09-26T10:00:00Z", "old@x", "9001")
            (dd / "sent-index.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

            queried_dates: list[str] = []

            def fake_run(args, account=None, timeout=30, max_retries=5):
                for a in args:
                    if a.startswith("date "):
                        queried_dates.append(a[len("date "):])
                return "[]"

            from datetime import datetime as real_datetime, timedelta

            frozen_today = real_datetime(2026, 9, 27)
            watermark_day = real_datetime(2026, 9, 26)

            class _FrozenDatetime(real_datetime):
                @classmethod
                def now(cls):
                    return real_datetime(2026, 9, 27, 12, 0, 0)

            with patch.object(sent_indexer, "run_himalaya", side_effect=fake_run), \
                 patch.object(sent_indexer, "datetime", _FrozenDatetime):
                sent_indexer.sync_sent_items(count=3, account="ACC", data_dir=dd)

            # Frozen today 2026-09-27 with a 2026-09-26 watermark: count=3 is
            # larger than the 2-day gap, so the base window is the inclusive
            # watermark..today span {2026-09-26, 2026-09-27}.  include_next_day
            # expands every base day to the following one, so the real queried
            # set is {2026-09-26, 2026-09-27, 2026-09-28}: today is the *last*
            # base day, NOT "today + 2" as the previous comment claimed.  The
            # expectation is derived from the frozen today so it stays valid if
            # the fixture date moves.
            base_days = [
                watermark_day + timedelta(days=i)
                for i in range((frozen_today - watermark_day).days + 1)
            ]
            expected = {
                (day + timedelta(days=offset)).strftime("%Y-%m-%d")
                for day in base_days
                for offset in (0, 1)
            }
            self.assertEqual(
                expected,
                set(queried_dates),
                f"count=3 must bound the window to {sorted(expected)}, queried {sorted(set(queried_dates))}",
            )
            self.assertIn("2026-09-27", queried_dates, "today always included")


class FailClosedSyncTests(unittest.TestCase):
    """Per-date Himalaya failures propagate instead of looking like 0 mails."""

    def test_himalaya_error_is_fail_closed_not_silent_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)

            def boom(args, account=None, timeout=30, max_retries=5):
                raise RuntimeError("himalaya timeout")

            with patch.object(sent_indexer, "run_himalaya", side_effect=boom):
                with self.assertRaises(Exception):
                    sent_indexer.sync_sent_items_by_date(
                        "2026-09-27", folder="Sent Items", account="ACC", data_dir=dd
                    )


class TelemetrySeparationTests(unittest.TestCase):
    """Sync mode reports date windows and real envelope counts separately."""

    def test_envelope_reports_date_windows_and_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dd = Path(tmp) / "data" / "mail-desk"
            dd.mkdir(parents=True)
            envelopes = [{"id": "e1", "subject": "s", "date": "Thu, 24 Sep 2026 10:00:00 +0200"}]

            def fake_run(args, account=None, timeout=30, max_retries=5):
                return json.dumps(envelopes, ensure_ascii=False)

            with patch.object(sent_indexer, "run_himalaya", side_effect=fake_run):
                with patch.object(sent_indexer, "get_single_email_details", side_effect=lambda *a, **k: {"raw_message_id": "r1", "in_reply_to": "", "references": [], "to": "x@example.test", "date": "Thu, 24 Sep 2026 10:00:00 +0200"}):
                    result = sync_sent_mode.run_sync_sent_mode(
                        {"mode": "sync_sent", "count": 150, "folder": "Sent Items"},
                        account="ACC",
                        data_dir=dd,
                    )
        data = result.get("data", result) if isinstance(result, dict) else {}
        self.assertIn(
            "date_windows_synced",
            json.dumps(result),
            "telemetry must separate date windows from envelope counts (FR-26/MD-SE2)",
        )
        self.assertIn("envelopes_examined", json.dumps(result))


if __name__ == "__main__":
    unittest.main()