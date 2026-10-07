"""The same event in the main and the family calendar is shown, counted and reminded once."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from test_jarvis import IntegrationBase


class DedupeTests(IntegrationBase):
    def add(self, event_id, calendar_id, summary, start, end, ical_uid="", attendees="[]", status="confirmed"):
        self.services.db.execute(
            "INSERT INTO events (event_id, calendar_id, path, summary, start, end, all_day, location, description, "
            "attendees, status, updated, html_link, ical_uid) VALUES (?, ?, ?, ?, ?, ?, 0, '', '', ?, ?, '1', '', ?)",
            (event_id, calendar_id, f"Sources/Calendar/{event_id}.md", summary, start, end, attendees, status, ical_uid))

    def test_family_copy_shown_once(self):
        s = self.services
        self.settings.google_calendar_ids = ["primary", "family@group.calendar.google.com"]
        tz = self.settings.tz
        tomorrow = datetime.now(tz).date() + timedelta(days=1)
        at = lambda h: datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, 0, tzinfo=tz).isoformat()  # noqa: E731
        emily = json.dumps([{"email": "emily@example.com", "name": "Emily"}])
        self.add("fam1", "family@group.calendar.google.com", "Dentist – Sam", at(9), at(10))
        self.add("main1", "primary", "Dentist - Sam", at(9), at(10))                 # same, typed slightly differently
        self.add("fam2", "family@group.calendar.google.com", "Parents evening", at(18), at(19), "uid-1", emily)
        self.add("main2", "primary", "Parents' evening (Hill School)", at(18), at(19), "uid-1")   # same invite
        self.add("main3", "primary", "Dentist - Sam", at(15), at(16))                 # another time: not the same
        self.assertEqual(s.calendar_pipeline.dedupe(), 2)
        dup = {r["event_id"]: r["duplicate_of"] for r in s.db.all("SELECT event_id, duplicate_of FROM events")}
        self.assertEqual(dup, {"fam1": "main1", "main1": "", "fam2": "main2", "main2": "", "main3": ""},
                         "the main calendar's copy is kept")
        self.assertEqual(s.calendar_pipeline.dedupe(), 0, "nothing new the second time")

        s.db.set("calendar.last_run", 1.0)
        events = self.run_async(s.assistant.calendar_events(tomorrow, tomorrow + timedelta(days=1)))
        self.assertEqual([e["event_id"] for e in events], ["main1", "main3", "main2"])
        from jarvis.pipelines.people import upcoming_meetings
        meetings = self.run_async(upcoming_meetings(s.people, days=3))
        self.assertEqual(len(meetings), 3)

        # Google's own copies (UTC, 'Z'), as a search or the first run returns them, are deduplicated too
        from jarvis.assistant.agenda import unique_events

        def utc(value):
            return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        live = [{"event_id": "fam1", "calendar_id": "family@group.calendar.google.com", "summary": "Dentist – Sam",
                 "start": utc(at(9)), "end": utc(at(10))},
                {"event_id": "main1", "calendar_id": "primary", "summary": "Dentist - Sam", "start": at(9), "end": at(10)}]
        order = {"primary": 0, "family@group.calendar.google.com": 1}
        self.assertEqual([e["event_id"] for e in unique_events(live, order)], ["main1"])

        # the main copy is deleted: the family one is shown again
        s.db.execute("UPDATE events SET status = 'cancelled' WHERE event_id = 'main1'")
        s.calendar_pipeline.dedupe()
        self.assertEqual(s.db.one("SELECT duplicate_of FROM events WHERE event_id = 'fam1'")["duplicate_of"], "")
        self.assertTrue(s.db.one("SELECT 1 FROM journal WHERE kind = 'event' AND ref = 'fam1'"))
