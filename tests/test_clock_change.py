"""The clocks go back (BST → GMT) on the last Sunday of October: times must stay right across it."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis.assistant.agenda import agenda_text, events_between
from jarvis.extract.events import local_iso
from jarvis.extract.when import next_occurrence, parse_when

UK = ZoneInfo("Europe/London")
CHANGE = date(2026, 10, 25)  # 02:00 BST → 01:00 GMT


class ClockChangeTests(unittest.TestCase):
    def test_daily_reminders_keep_their_clock_time(self):
        at = datetime(2026, 10, 24, 7, 0, tzinfo=UK)  # 07:00 BST
        after = next_occurrence(at, "daily")
        self.assertEqual((after.date(), after.hour, after.minute), (CHANGE, 7, 0))
        self.assertEqual(after.utcoffset(), timedelta(0), "07:00 GMT the day after the change")
        self.assertEqual(after.timestamp() - at.timestamp(), 25 * 3600)
        weekly = next_occurrence(datetime(2026, 10, 20, 18, 30, tzinfo=UK), "weekly")
        self.assertEqual((weekly.hour, weekly.minute), (18, 30))

    def test_relative_reminders_count_real_time(self):
        now = datetime(2026, 10, 25, 0, 30, tzinfo=UK)  # BST, two hours before the clocks go back
        when = parse_when("remind me in 2 hours to check the oven", now)
        self.assertEqual(when.at.timestamp() - now.timestamp(), 2 * 3600)
        self.assertEqual(when.at.astimezone(UK).hour, 1, "01:30 GMT")
        days = parse_when("remind me in 2 days to call Sam", datetime(2026, 10, 24, 9, 0, tzinfo=UK))
        self.assertEqual(days.at.astimezone(UK).hour, 9, "days keep the clock time")

    def test_google_utc_times_are_stored_as_local_days(self):
        self.assertEqual(local_iso("2026-10-24T23:30:00Z", UK), "2026-10-25T00:30:00+01:00", "BST: next day")
        self.assertEqual(local_iso("2026-10-25T23:30:00Z", UK), "2026-10-25T23:30:00+00:00", "GMT: same day")
        self.assertEqual(local_iso("2026-10-25", UK), "2026-10-25")
        self.assertEqual(local_iso("", UK), "")

    def test_agenda_on_the_day_the_clocks_change(self):
        events = [
            {"event_id": "a", "summary": "Church", "start": "2026-10-25T10:00:00Z", "end": "2026-10-25T11:30:00Z"},
            {"event_id": "b", "summary": "Late film", "start": "2026-10-24T23:15:00Z",
             "end": "2026-10-25T01:30:00Z"},  # 00:15 BST – 01:30 GMT
            {"event_id": "c", "summary": "Saturday tea", "start": "2026-10-24T16:00:00Z",
             "end": "2026-10-24T17:00:00Z"},
        ]
        found = events_between(events, CHANGE, CHANGE + timedelta(days=1), UK)
        text = agenda_text(found, CHANGE, CHANGE + timedelta(days=1), "", date(2026, 10, 20))
        self.assertIn("- 00:15–01:30 Late film", text)
        self.assertIn("- 10:00–11:30 Church", text, "10:00 GMT is 10:00 UK time after the change")
        self.assertNotIn("Saturday tea", text)


if __name__ == "__main__":
    unittest.main()
