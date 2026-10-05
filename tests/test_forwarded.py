"""Forwarded emails keep what was forwarded; emails listing several dates become several events."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from starlette.testclient import TestClient

from jarvis.auth import Auth
from jarvis.extract.event_text import parse_date_list
from jarvis.google.gmail import clean_body, parse_message
from jarvis.web.app import create_app
from test_events_voice import FakeCalendarAPI
from test_jarvis import FakeGmail, IntegrationBase, gmail_message

GMAIL_FORWARD = """Thought you'd want this in the calendar.

Chris
--
Sent from my phone

---------- Forwarded message ---------
From: Hippodrome Youth Theatre <hello@hippodrome.org.uk>
Date: Mon, 5 Oct 2026 at 09:12
Subject: Rehearsal schedule
To: Sophie Key <sophie@example.com>

Hi all, rehearsals for Annie are:
{d1:%a %d %b} 10:00-12:00
{d2:%a %d %b} 10:00-12:00
{d3:%a %d %b} 14:00-17:00 (dress rehearsal)

See you there!
"""


class ForwardParsing(IntegrationBase.__mro__[1]):
    def test_forwarded_text_is_kept(self):
        outlook = ("FYI\n\nFrom: Smiles Dental <reception@smiles.co.uk>\nSent: Monday, October 5, 2026 10:00 AM\n"
                   "To: Chris\nSubject: Appointment confirmation\n\nYour appointment is on Tuesday 13 October at 9:30am.\n")
        self.assertIn("Your appointment is on Tuesday 13 October", clean_body(outlook, 8000, "FW: Appointment"))
        self.assertIn("--- Forwarded email from Smiles Dental <reception@smiles.co.uk>, sent Monday, October 5, 2026 "
                      "10:00 AM: Appointment confirmation ---", clean_body(outlook, 8000, "FW: Appointment"))
        apple = ("Begin forwarded message:\n\nFrom: Amazon <ship@amazon.co.uk>\nSubject: Ready to collect\n"
                 "Date: 5 October 2026 at 10:00:00 BST\nTo: chris@example.com\n\nYour parcel is ready for pickup.\n")
        self.assertIn("Your parcel is ready for pickup.", clean_body(apple, 8000, "Fwd: Ready to collect"))
        # replies still drop the quoted conversation
        reply = "Sounds good.\n\nOn Mon, 5 Oct 2026 at 10:00, Bob <bob@x.com> wrote:\n> Can we meet Friday?\n"
        self.assertEqual(clean_body(reply, 8000, "Re: meeting"), "Sounds good.")
        outlook_reply = "Yes.\n\n-----Original Message-----\nFrom: Bob\nSent: Monday\nSubject: x\n\nOld stuff\n"
        self.assertEqual(clean_body(outlook_reply, 8000, "RE: x"), "Yes.")

    def test_dates_listed_in_an_email(self):
        now = datetime(2026, 10, 5, 10, tzinfo=self.settings_tz())
        events = parse_date_list("Fwd: School term dates", "Term dates 2026/27\nAutumn half term: 26/10/2026 - "
                                 "30/10/2026\nINSET day 2 November 2026\nPerformances: 13/11/2026 7pm and 14/11/2026 "
                                 "2:30pm\nPaid £5 on 01/11/2026\n", now)
        self.assertEqual([(e.title, e.start[:16], e.end[:10], e.all_day) for e in events], [
            ("School term dates — Autumn half term", "2026-10-26", "2026-10-31", True),
            ("School term dates — INSET day", "2026-11-02", "2026-11-03", True),
            ("School term dates — Performances", "2026-11-13T19:00", "2026-11-13", False),
            ("School term dates — Performances", "2026-11-14T14:30", "2026-11-14", False)])
        self.assertEqual(parse_date_list("Lunch", "See you on 14/10/2026 at 1pm!", now), [], "one date isn't a list")

    @staticmethod
    def settings_tz():
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/London")


class ForwardFlow(IntegrationBase):
    def setUp(self):
        super().setUp()
        s = self.services
        s.oauth.has_scope = lambda scope: True
        self.fake_cal = FakeCalendarAPI(s.oauth)
        s.events.calendar = s.calendar_pipeline.calendar = s.assistant.calendar = self.fake_cal

    def test_forwarded_schedule_to_calendar(self):
        s = self.services
        today = datetime.now(self.settings.tz).date()
        days = [today + timedelta(days=n) for n in (6, 13, 20)]
        # forwarded to yourself: Gmail labels it SENT and INBOX
        forward = gmail_message("f1", "tf1", "Chris <chris@example.com>", "Fwd: Rehearsal schedule",
                                GMAIL_FORWARD.format(d1=days[0], d2=days[1], d3=days[2]), labels=["SENT", "INBOX"])
        parsed = parse_message(forward, 8000)
        self.assertTrue(parsed.outgoing)
        self.assertEqual((parsed.forwarded_from, parsed.sender_name), ("hello@hippodrome.org.uk", "Hippodrome Youth Theatre"))
        self.assertIn("Hi all, rehearsals for Annie are:", parsed.body)
        self.assertNotIn("Sent from my phone", parsed.body)
        s.gmail_pipeline.gmail = s.events.gmail = FakeGmail([forward])
        s.db.set("gmail.history_id", "1")

        async def history(start):
            return ["f1"], "2"
        s.gmail_pipeline.gmail.history = history
        result = self.run_async(s.gmail_pipeline.run())
        self.assertEqual(result["event_proposals"], 3, result)
        pending = sorted(s.events.list("pending"), key=lambda p: p["start"])
        self.assertEqual([p["title"] for p in pending], ["Rehearsal schedule", "Rehearsal schedule",
                                                         "Rehearsal schedule — Dress rehearsal"])
        self.assertEqual(pending[0]["start"][:16], f"{days[0]}T10:00")
        notes = [r["title"] for r in s.db.all("SELECT title FROM notifications WHERE title LIKE 'Add to calendar?%'")]
        self.assertEqual(notes, ["Add to calendar? 3 dates from “Fwd: Rehearsal schedule”"], "one notification")
        # Add all
        app = create_app(self.settings, s, start_jobs=False)
        Auth(s.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            result = client.post("/api/events/add-all", json={"ids": [p["id"] for p in pending]}, headers=h).json()
        self.assertEqual(result, {"added": 3, "errors": []})
        self.assertEqual(len(self.fake_cal.inserted), 3)

    def test_forwarded_parcel_and_renewal(self):
        s = self.services
        soon = datetime.now(self.settings.tz).date() + timedelta(days=10)
        parcel = gmail_message("f2", "tf2", "Chris <chris@example.com>", "Fwd: Your parcel is ready to collect",
                               "---------- Forwarded message ---------\nFrom: InPost <noreply@inpost.co.uk>\n"
                               "Date: Mon, 5 Oct 2026\nSubject: Your parcel is ready to collect\n\n"
                               "Your parcel is ready to collect from the InPost Locker at Tesco Extra. "
                               "Your collection code is 482915.", labels=["SENT", "INBOX"])
        renewal = gmail_message("f3", "tf3", "Chris <chris@example.com>", "Fwd: Your home insurance renewal",
                                "---------- Forwarded message ---------\nFrom: Admiral <noreply@admiral.com>\n"
                                "Subject: Renewal\n\nYour home insurance policy renews on "
                                f"{soon.day} {soon:%B %Y}.", labels=["SENT", "INBOX"])
        s.deliveries.on_message(parse_message(parcel, 8000))
        ready = [d for d in s.deliveries.active() if d["status"] == "ready_to_collect"]
        self.assertEqual(len(ready), 1)
        self.assertEqual((ready[0]["retailer"], ready[0]["collect_code"]), ("InPost", "482915"))
        s.deadlines.on_message(parse_message(renewal, 8000))
        self.assertEqual([(d["org"], d["due"]) for d in s.deadlines.upcoming()], [("Admiral", soon.isoformat())])
