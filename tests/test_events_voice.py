"""Events-from-email, vault save reports and voice.  PYTHONPATH=src:tests python -m unittest discover -s tests"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from fakes import FakeKokoro, Server
from jarvis.auth import Auth
from jarvis.extract.events import parse_ics, parse_jsonld, validate_llm_events, worth_llm_scan
from jarvis.google.gmail import parse_message
from jarvis.web.app import create_app
from test_jarvis import FakeGmail, IntegrationBase, b64, gmail_message

TZ = ZoneInfo("Europe/London")
FUTURE = datetime.now(TZ) + timedelta(days=10)

ICS = f"""BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:abc-123@example.com
DTSTART;TZID=Europe/London:{FUTURE:%Y%m%d}T190000
DTEND;TZID=Europe/London:{FUTURE:%Y%m%d}T210000
SUMMARY:Sam's birthday drinks\\, The Crown
LOCATION:The Crown\\, Leeds
DESCRIPTION:Bring a card
 and a smile
END:VEVENT
END:VCALENDAR
"""

JSONLD = f"""<html><head><script type="application/ld+json">
{{"@context":"http://schema.org","@type":"FoodEstablishmentReservation","reservationNumber":"X1",
 "startTime":"{FUTURE:%Y-%m-%d}T12:30:00+01:00",
 "reservationFor":{{"@type":"FoodEstablishment","name":"Dishoom","address":{{"streetAddress":"1 High St","addressLocality":"Manchester"}}}}}}
</script></head><body>Your table is booked</body></html>"""


class ExtractTests(IntegrationBase.__mro__[1]):  # plain unittest.TestCase
    def test_ics(self):
        events = parse_ics(ICS, TZ)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.title, "Sam's birthday drinks, The Crown")
        self.assertEqual(e.location, "The Crown, Leeds")
        self.assertEqual(e.notes, "Bring a cardand a smile")
        self.assertEqual(e.ical_uid, "abc-123@example.com")
        self.assertTrue(e.start.startswith(f"{FUTURE:%Y-%m-%d}T19:00"))
        self.assertFalse(e.all_day)
        cancelled = ICS.replace("METHOD:REQUEST", "METHOD:CANCEL")
        self.assertEqual(parse_ics(cancelled, TZ), [])
        all_day = parse_ics("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20301224\nSUMMARY:Xmas Eve\nEND:VEVENT", TZ)[0]
        self.assertTrue(all_day.all_day)
        self.assertEqual((all_day.start, all_day.end), ("2030-12-24", "2030-12-25"))
        # STATUS line (Outlook/Google invites) and a VALARM block inside the event
        confirmed = ICS.replace("END:VEVENT", "STATUS:CONFIRMED\nBEGIN:VALARM\nACTION:DISPLAY\n"
                                "DESCRIPTION:Reminder\nEND:VALARM\nEND:VEVENT")
        events = parse_ics(confirmed, TZ)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].notes, "Bring a cardand a smile")
        self.assertEqual(parse_ics(ICS.replace("END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT"), TZ), [])

    def test_ics_from_abroad_with_quoted_parameters(self):
        try:
            import icalendar  # noqa: F401 — the built-in fallback doesn't handle these cases (the Docker test stage has it)
        except ImportError:
            self.skipTest("icalendar not installed here")
        ics = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x1\r\nSUMMARY:Call with Boston\r\n'
               'DTSTART;TZID="America/New_York":20301015T190000\r\nDTEND;TZID="America/New_York":20301015T200000\r\n'
               'LOCATION;ALTREP="https://example.com/a:b":Zoom\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        event = parse_ics(ics, TZ)[0]
        self.assertEqual(event.start[:16], "2030-10-16T00:00")  # 7pm in New York is midnight in London
        self.assertEqual(event.location, "Zoom")

    def test_jsonld(self):
        events = parse_jsonld(JSONLD, TZ)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "Table at Dishoom")
        self.assertIn("Manchester", events[0].location)

    def test_llm_validation(self):
        now = datetime.now(TZ)
        raw = json.dumps({"events": [
            {"title": "Match", "start": f"{FUTURE:%Y-%m-%d}T10:00", "confidence": 0.8},
            {"title": "Low", "start": f"{FUTURE:%Y-%m-%d}T10:00", "confidence": 0.2},
            {"title": "Past", "start": "2020-01-01T10:00", "confidence": 0.9},
            {"title": "Garbage", "start": "next week", "confidence": 0.9},
        ]})
        events = validate_llm_events(raw, TZ, now)
        self.assertEqual([e.title for e in events], ["Match"])
        self.assertTrue(events[0].end > events[0].start)
        self.assertEqual(validate_llm_events("not json", TZ, now), [])

    def test_prefilter(self):
        self.assertTrue(worth_llm_scan("Parents evening", "It is on Tuesday 14 October at 6pm")[0])
        self.assertFalse(worth_llm_scan("Hello", "Thanks for the photos!")[0])
        vinted = ("This October, we're updating our Terms and Conditions (T&Cs). Please make sure the details match "
                  "your account. These updated T&Cs apply from 8 October 2026. Until then, our current T&Cs apply. "
                  "Got questions? Ask them here.")
        self.assertFalse(worth_llm_scan("T&Cs updates", vinted)[0])
        self.assertFalse(worth_llm_scan("T&Cs updates", vinted, automated=True)[0])
        self.assertFalse(worth_llm_scan("Your statement is ready", "Due by 12 October", automated=True)[0])
        self.assertTrue(worth_llm_scan("Your appointment on 14 October", "See you at 10:30am for your appointment",
                                       automated=True)[0])


class FakeCalendarAPI:
    """Stands in for google.calendar.Calendar (events() + insert())."""

    def __init__(self, oauth):
        self.oauth = oauth
        self.inserted: list[dict] = []

    async def events(self, *a, **k):
        return []

    async def upcoming(self, *a, **k):
        return []

    async def calendars(self):
        return [{"id": "chris@example.com", "name": "Chris", "primary": True, "writable": True, "hidden": False},
                {"id": "fam123@group.calendar.google.com", "name": "Family", "primary": False, "writable": True,
                 "hidden": True},
                {"id": "work@group.calendar.google.com", "name": "Old work", "primary": False, "writable": True,
                 "hidden": True},
                {"id": "holidays", "name": "UK Holidays", "primary": False, "writable": False, "hidden": False}]

    async def get_event(self, calendar_id, event_id):
        return {"id": event_id, "description": "Lane 4", "htmlLink": "https://calendar.google.com/event?eid=b"}

    async def patch(self, calendar_id, event_id, body):
        self.patched = getattr(self, "patched", []) + [(calendar_id, event_id, body)]
        return {"id": event_id}

    async def insert(self, calendar_id, body):
        self.inserted.append(body)
        self.calendar_ids = getattr(self, "calendar_ids", []) + [calendar_id]
        return {"id": f"new{len(self.inserted)}", "htmlLink": "https://calendar.google.com/event?eid=x"}


class EventFlowTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        s = self.services
        s.oauth.has_scope = lambda scope: True
        self.fake_cal = FakeCalendarAPI(s.oauth)
        s.events.calendar = self.fake_cal
        s.calendar_pipeline.calendar = self.fake_cal
        s.assistant.calendar = self.fake_cal
        self.ollama.event_start = f"{FUTURE:%Y-%m-%d}T18:30"

    def invite(self, mid="i1"):
        message = gmail_message(mid, "t" + mid, "Sam Jones <sam@example.com>", "Invitation: birthday drinks",
                                "You are invited")
        message["payload"]["parts"].append({"mimeType": "text/calendar", "body": {"data": b64(ICS)}})
        return message

    def test_email_to_calendar_flow(self):
        s = self.services
        booking = gmail_message("b1", "tb1", "Dishoom <noreply@dishoom.com>", "Your booking", "Your table is booked",
                                labels=["INBOX", "CATEGORY_UPDATES"])
        booking["payload"]["parts"][1]["body"]["data"] = b64(JSONLD)
        school = gmail_message("s1", "ts1", "Hill School <office@hill.sch.uk>", "Parents evening",
                               "Parents evening is on Tuesday at 6:30pm in the hall.")
        promo = gmail_message("p1", "tp1", "Shop <deals@shop.com>", "Sale event Saturday 10am", "Big sale event",
                              labels=["CATEGORY_PROMOTIONS"])
        s.gmail_pipeline.gmail = s.events.gmail = FakeGmail([self.invite(), booking, school, promo])
        s.db.set("gmail.history_id", "1")  # incremental mode (notifications on)
        s.gmail_pipeline.gmail.history = self._history(["i1", "b1", "s1", "p1"])
        result = self.run_async(s.gmail_pipeline.run())
        self.assertEqual(result["event_proposals"], 2, result)  # ics + json-ld; school goes to the model queue
        queued = s.db.one("SELECT COUNT(*) n FROM event_scan WHERE status = 'pending'")["n"]
        self.assertEqual(queued, 1)
        scan = self.run_async(s.scan_events())
        self.assertEqual(scan["proposed"], 1, scan)
        pending = s.events.list("pending")
        self.assertEqual(sorted(p["source"] for p in pending), ["ics", "jsonld", "llm"])
        notes = s.db.all("SELECT title FROM notifications")
        self.assertEqual(sum(n["title"].startswith("Add to calendar?") for n in notes), 3)
        # re-seeing the same invite does not create another proposal
        self.assertEqual(self.run_async(s.events.on_message(parse_message(self.invite("i2")))), 0)
        # accept one with an edit, dismiss another
        ics = next(p for p in pending if p["source"] == "ics")
        added = self.run_async(s.events.accept(ics["id"], {"title": "Sam's birthday"}))
        self.assertEqual(added["status"], "added")
        body = self.fake_cal.inserted[0]
        self.assertEqual(body["summary"], "Sam's birthday")
        self.assertEqual(body["start"]["timeZone"], "Europe/London")
        self.assertIn("mail.google.com", body["description"])
        # links open the thread on the right Google account, not whichever is signed in first (/u/0/)
        s.db.set("gmail.me", "chris@example.com")
        card = s.events.get(ics["id"])
        self.assertIn("mail.google.com/mail/u/chris@example.com/#all/t", card["gmail_url"])
        self.assertEqual(card["email_url"], f"/#email?thread={card['thread_id']}")  # opens inside Jarvis
        # the email reader inside Jarvis (from the stored copy when Gmail can't be asked)
        thread = self.run_async(s.email_thread(card["thread_id"]))
        self.assertEqual(thread["subject"], "Invitation: birthday drinks")
        self.assertIn("You are invited", thread["messages"][0]["body"])
        llm = next(p for p in pending if p["source"] == "llm")
        s.events.dismiss(llm["id"])
        self.assertEqual(len(s.events.list("pending")), 1)
        journal = s.db.one("SELECT text FROM journal WHERE kind = 'calendar-add'")["text"]
        self.assertIn("Added to calendar", journal)

    def test_notice_emails_and_sender_muting(self):
        s = self.services
        vinted = gmail_message("v1", "tv1", "Vinted <no-reply@vinted.co.uk>", "T&Cs updates",
                               "These updated T&Cs apply from 8 October 2026. Until then, our current T&Cs apply.",
                               labels=["INBOX", "CATEGORY_UPDATES"])
        self.assertEqual(self.run_async(s.events.on_message(parse_message(vinted))), 0)
        self.assertIsNone(s.db.one("SELECT 1 FROM event_scan WHERE message_id = 'v1'"))
        # a sender whose suggestions keep getting dismissed is muted after two
        message = gmail_message("c1", "tc1", "Club <club@example.com>", "Parents evening",
                                "Parents evening is on Tuesday at 6:30pm in the hall.")
        self.run_async(s.events.on_message(parse_message(message)))
        self.ollama.event_start = f"{FUTURE:%Y-%m-%d}T18:30"
        self.run_async(s.scan_events())
        club = [p for p in s.events.list("pending") if p["sender"] == "club@example.com"]
        self.assertTrue(club, s.events.list("pending"))
        first = s.events.dismiss(club[0]["id"])
        self.assertFalse(first["muted"])
        s.db.execute("INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, location, "
                     "notes, confidence, status, sender, email_subject) VALUES (0, 'llm', 'x2', 'Other', ?, '', 0, '', '', "
                     "0.9, 'pending', 'club@example.com', 'Club dinner 3')", (f"{FUTURE:%Y-%m-%d}T19:00",))
        extra = s.db.one("SELECT id FROM event_proposals WHERE fingerprint = 'x2'")["id"]
        s.db.execute("INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, location, "
                     "notes, confidence, status, sender, email_subject) VALUES (0, 'llm', 'x3', 'More', ?, '', 0, '', '', "
                     "0.9, 'pending', 'club@example.com', 'Club dinner 4')", (f"{FUTURE:%Y-%m-%d}T20:00",))
        second = s.events.dismiss(extra)
        self.assertTrue(second["muted"])
        self.assertTrue(s.events.is_muted("Club@Example.com"))
        self.assertFalse([p for p in s.events.list("pending") if p["sender"] == "club@example.com"])
        later = gmail_message("c9", "tc9", "Club <club@example.com>", "Club dinner", "Dinner is on Friday at 7pm.")
        self.run_async(s.events.on_message(parse_message(later)))
        self.assertIsNone(s.db.one("SELECT 1 FROM event_scan WHERE message_id = 'c9'"))
        self.assertEqual([m["sender"] for m in s.events.muted_senders()], ["club@example.com"])
        s.events.unmute("club@example.com")
        self.assertFalse(s.events.is_muted("club@example.com"))
        # explicit "not from this sender" mutes at once; startup cleanup removes old notice suggestions
        s.db.execute("INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, location, "
                     "notes, confidence, status, sender, email_subject) VALUES (0, 'llm', 'x4', 'New T&Cs', ?, '', 1, '', "
                     "'', 0.9, 'pending', 'no-reply@vinted.co.uk', 'T&Cs updates')", (f"{FUTURE:%Y-%m-%d}",))
        self.assertEqual(s.events.cleanup_noise(), 1)
        s.db.execute("INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, location, "
                     "notes, confidence, status, sender, email_subject) VALUES (0, 'llm', 'x5', 'Sale', ?, '', 1, '', "
                     "'', 0.9, 'pending', 'shop@example.com', 'Weekend plans')", (f"{FUTURE:%Y-%m-%d}",))
        x5 = s.db.one("SELECT id FROM event_proposals WHERE fingerprint = 'x5'")["id"]
        self.assertTrue(s.events.dismiss(x5, mute=True)["muted"])

    @staticmethod
    def _history(ids):
        async def history(start):
            return ids, "2"
        return history

    def test_chat_calendar_add_and_scan(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)

            def chat(message):
                response = client.post("/api/chat", json={"message": message}, headers=h)
                return [json.loads(line) for line in response.text.splitlines() if line.strip()]

            events = chat("Add dentist on Friday at 6.30pm to my calendar")
            proposals = next(e for e in events if e["type"] == "proposals")["items"]
            self.assertEqual(proposals[0]["title"], "Dentist")
            response = client.post(f"/api/events/{proposals[0]['id']}/add", json={}, headers=h)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "added")
            events = chat("Check my emails for events I need to add to my calendar")
            self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "events")
            listing = client.get("/api/events?status=added").json()
            self.assertEqual(len(listing["items"]), 1)
            # [[Sources/Email/…]] links in chat open the email inside Jarvis
            self.services.db.execute("INSERT INTO threads (thread_id, path, subject, first_ts) VALUES "
                                     "('18c2f3a9b', 'Sources/Email/2026/09/House move.md', 'House move', 1)")
            self.services.db.execute(
                "INSERT INTO emails (message_id, thread_id, ts, from_addr, from_name, to_addrs, subject, labels, bulk, "
                "outgoing, snippet, body, attachments) VALUES ('m9', '18c2f3a9b', 1, 'sam@example.com', 'Sam', '[]', "
                "'House move', '[]', 0, 0, '', 'Moving to Leeds', '[]')")
            email = client.get("/api/email/note?path=Sources/Email/2026/09/House move", headers=h).json()
            self.assertEqual((email["subject"], email["messages"][0]["body"]), ("House move", "Moving to Leeds"))
            self.assertEqual(client.get("/api/email/not-hex!", headers=h).status_code, 400)

    def test_named_calendar(self):
        s = self.services
        s.calendar = self.fake_cal
        # Family is hidden in Google Calendar: not offered until ticked on the Status page
        events = self.run_async(self._ask("Add lunch to the Family calendar on Friday at 1pm"))
        self.assertIn("couldn't find a calendar called “Family”", "".join(e.get("text", "") for e in events))
        choices = {c["name"]: c for c in self.run_async(s.calendar_choices())}
        self.assertEqual((choices["Chris"]["enabled"], choices["Family"]["enabled"]), (True, False))
        s.set_calendars(["primary", "fam123@group.calendar.google.com"])
        self.assertEqual(s.settings.google_calendar_ids, ["primary", "fam123@group.calendar.google.com"])
        self.assertEqual(s.db.get("calendars.enabled"), ["primary", "fam123@group.calendar.google.com"])
        # the card offers the ticked calendars you can write to, main one first
        targets = self.run_async(s.events.target_calendars())
        self.assertEqual([c["name"] for c in targets], ["Chris", "Family"])
        card = next(e for e in self.run_async(self._ask("Add dentist on Friday at 3pm to my calendar"))
                    if e["type"] == "proposals")["items"][0]
        from jarvis.google.oauth import GoogleError
        with self.assertRaises(GoogleError):  # a hidden calendar you haven't ticked can't be chosen
            self.run_async(s.events.accept(card["id"], {"calendar_id": "work@group.calendar.google.com"}))
        added = self.run_async(s.events.accept(card["id"], {"calendar_id": "fam123@group.calendar.google.com"}))
        self.assertEqual((self.fake_cal.calendar_ids[-1], added["calendar_name"]),
                         ("fam123@group.calendar.google.com", "Family"))
        self.fake_cal.inserted.clear()
        self.fake_cal.calendar_ids = []

        async def ask(text):
            return [e async for e in s.assistant.handle(text)]
        events = self.run_async(ask("Add Wine and Zoom with Ben and Emily Topliss to the Family calendar on "
                                    "Thursday at 8:30pm"))
        self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "calendar-add")
        card = next(e for e in events if e["type"] == "proposals")["items"][0]
        self.assertEqual((card["title"], card["calendar_name"]), ("Wine and Zoom with Ben and Emily Topliss", "Family"))
        self.assertEqual(card["start"][11:16], "20:30")
        self.assertEqual(self.fake_cal.inserted, [], "nothing added before you confirm")
        self.run_async(s.events.accept(card["id"]))
        self.assertEqual(self.fake_cal.calendar_ids, ["fam123@group.calendar.google.com"])
        events = self.run_async(ask("Add pottery to the Pottery calendar on Friday at 7pm"))
        text = "".join(e.get("text", "") for e in events if e["type"] == "token")
        self.assertIn("couldn't find a calendar called “Pottery”", text)
        self.assertIn("Chris, Family", text)  # read-only calendars aren't offered
        self.assertNotIn("Old work", text)     # nor hidden ones you haven't ticked

    async def _collect(self, generator):
        return [e async for e in generator]

    def _ask(self, text):
        return self._collect(self.services.assistant.handle(text))

    def test_several_actions_in_one_message(self):
        from datetime import datetime as dt, timedelta as td
        s = self.services
        now = dt.now(s.settings.tz)
        saturday = (now + td(days=(5 - now.weekday()) % 7 or 7)).date()
        s.db.execute("INSERT INTO events (event_id, calendar_id, path, summary, start, end, all_day, location, description, "
                     "attendees, status, updated, html_link) VALUES ('bowl1', 'fam@group', '', 'Bowling', ?, ?, 0, "
                     "'Hollywood Bowl', '', '[]', 'confirmed', '', '')",
                     (f"{saturday}T17:00:00+01:00", f"{saturday}T19:00:00+01:00"))
        events = self.run_async(self._ask("Add booking reference is 203BIR-NGC5GHR to bowling on Saturday and remind "
                                          "me of it at 4:30 so I have it ready"))
        self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "multi")
        note = next(e for e in events if e["type"] == "proposals")["items"][0]
        self.assertEqual((note["kind"], note["title"], note["notes"]),
                         ("note", "Bowling", "Booking reference: 203BIR-NGC5GHR"))
        reminder = next(e for e in events if e["type"] == "actions")["items"][0]
        due = dt.fromtimestamp(reminder["due"], s.settings.tz)
        self.assertEqual((due.date(), due.strftime("%H:%M")), (saturday, "16:30"))  # Saturday, and pm
        self.assertIn("203BIR-NGC5GHR", reminder["text"])
        text = "".join(e.get("text", "") for e in events if e["type"] == "token")
        self.assertIn("**1. Add booking reference", text)
        self.assertIn("**2. remind me of the bowling booking reference 203BIR-NGC5GHR", text)
        self.assertNotIn("haven't actually changed", text)
        saved = s.db.all("SELECT role FROM chat_messages WHERE role IN ('user', 'assistant')")
        self.assertEqual(len(saved), 2)  # the whole request is one turn in the history
        self.assertEqual(self.fake_cal.__dict__.get("patched"), None, "nothing changed before you confirm")
        result = self.run_async(s.events.accept(note["id"]))
        self.assertEqual(result["status"], "added")
        self.assertEqual(self.fake_cal.patched,
                         [("fam@group", "bowl1", {"description": "Lane 4\n\nBooking reference: 203BIR-NGC5GHR"})])
        self.assertEqual(s.history()[0]["status"], "details added")
        # an event that isn't there is explained rather than guessed
        events = self.run_async(self._ask("Add the table number 12 to curling on Saturday"))
        self.assertIn("couldn't find a “curling” event", "".join(e.get("text", "") for e in events))

    def test_claimed_actions_are_flagged(self):
        from jarvis.assistant.core import CLAIMED_ACTION
        self.assertTrue(CLAIMED_ACTION.search("I've added \"Wine and Zoom\" to the Family calendar"))
        self.assertFalse(CLAIMED_ACTION.search("You have 3 events on Thursday."))

    def test_save_reports(self):
        s = self.services
        self.run_async(s.writer.refresh_people())
        s.gmail_pipeline.gmail = FakeGmail([
            gmail_message("m1", "t1", "Sam Jones <sam@example.com>", "House move", "Moving to Leeds in November")])
        self.run_async(s.gmail_pipeline.run())
        self.run_async(s.writer.flush())
        activity = s.db.one("SELECT content FROM chat_messages WHERE role = 'activity'")["content"]
        self.assertIn("Saved to your vault", activity)
        self.assertIn("Email “House move” — 1 message(s), latest from Sam Jones", activity)
        self.assertIn("Updated: [[People/Sam Jones|Sam Jones]]", activity)
        self.assertIn("New: [[Journal/", activity)
        self.assertEqual(self.run_async(s.notify_saves()), "off")  # save notifications are off by default
        s.db.execute("UPDATE vault_saves SET notified = 0")
        s.notifier.set_preferences({"saves": {"on": True}})
        self.assertEqual(self.run_async(s.notify_saves()), "notified 3")
        self.assertEqual(self.run_async(s.notify_saves()), "nothing new")
        note = s.db.one("SELECT * FROM notifications WHERE title LIKE 'Jarvis saved%'")
        self.assertEqual(note["priority"], 2)
        # activity is never sent to the model as conversation history
        self.assertEqual([m["role"] for m in s.assistant.history()], [])
        # an unchanged re-render reports nothing
        s.db.queue_note("person", "sam@example.com")
        self.assertEqual(self.run_async(s.writer.flush())["saved"], [])


class VoiceTests(IntegrationBase):
    def test_browser_fallback_and_kokoro(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            self.assertEqual(client.get("/api/session").json()["tts"], "browser")
            self.assertEqual(client.post("/api/tts", json={"text": "hi"}, headers=h).status_code, 409)
            kokoro = FakeKokoro()
            with Server(kokoro.app()) as server:
                self.settings.tts_provider = "kokoro"
                self.settings.tts_url = server.url
                response = client.post("/api/tts", json={"text": "**Hello** [[People/Sam Jones|Sam]] — see "
                                                                 "https://x.y"}, headers=h)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "audio/mpeg")
                self.assertEqual(kokoro.requests[0]["input"], "Hello Sam , see")
                self.assertEqual(kokoro.requests[0]["voice"], "bm_george")
            self.assertIn("media-src 'self' blob:", client.get("/").headers["content-security-policy"])


class FakeWhisper:
    """OpenAI-compatible /v1/audio/transcriptions."""

    def __init__(self):
        self.requests = []

    def app(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, PlainTextResponse
        from starlette.routing import Route

        async def transcribe(request):
            form = await request.form()
            upload = form["file"]
            self.requests.append({"model": form["model"], "language": form["language"], "filename": upload.filename,
                                  "bytes": len(await upload.read())})
            return JSONResponse({"text": " What's on tomorrow? "})

        async def health(request):
            return PlainTextResponse("OK")
        return Starlette(routes=[Route("/v1/audio/transcriptions", transcribe, methods=["POST"]),
                                 Route("/health", health)])


class HearingTests(IntegrationBase):
    def test_whisper_and_phone_fallback(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            self.assertEqual(client.get("/api/session").json()["stt"], "browser")
            response = client.post("/api/stt", content=b"audio", headers=h | {"Content-Type": "audio/webm"})
            self.assertEqual((response.status_code, response.json()["fallback"]), (503, True))
            self.assertIn("microphone=(self)", client.get("/").headers["permissions-policy"])
            whisper = FakeWhisper()
            with Server(whisper.app()) as server:
                self.settings.stt_url = server.url
                self.assertEqual(client.get("/api/session").json()["stt"], "server")
                self.assertEqual(client.post("/api/stt", content=b"audio", headers={"Content-Type": "audio/webm"})
                                 .status_code, 403, "needs the app's header")
                response = client.post("/api/stt", content=b"x" * 2000,
                                       headers=h | {"Content-Type": "audio/webm;codecs=opus"})
                self.assertEqual(response.json(), {"text": "What's on tomorrow?"})
                self.assertEqual(whisper.requests[0], {"model": "Systran/faster-whisper-small.en", "language": "en",
                                                       "filename": "speech.webm", "bytes": 2000})
                self.assertEqual(client.post("/api/stt", content=b"", headers=h).status_code, 400)
                self.assertTrue(client.get("/api/status").json()["components"]["hearing"]["ok"])
            self.settings.stt_url = f"http://127.0.0.1:{__import__('test_jarvis').free_port()}"  # PC off
            response = client.post("/api/stt", content=b"audio", headers=h | {"Content-Type": "audio/webm"})
            self.assertEqual((response.status_code, response.json()["fallback"]), (503, True))
            self.assertIn("phone recognition", client.get("/api/status").json()["components"]["hearing"]["detail"])


class EventTextTests(IntegrationBase.__mro__[1]):
    def test_dates_and_times_in_calendar_requests(self):
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        from jarvis.extract.event_text import parse_event_request
        now = dt(2026, 9, 29, 21, 0, tzinfo=ZoneInfo("Europe/London"))
        cases = {
            "Add to calendar, 31/10/2026: Rayner's Christmas birthday ham party, 7pm":
                ("Rayner's Christmas birthday ham party", "2026-10-31T19:00", "2026-10-31T20:00"),
            "add school play 3.11.26 at 14.30 to calendar": ("School play", "2026-11-03T14:30", "2026-11-03T15:30"),
            "Put 2026-11-05 bonfire night at 18:30 in my diary": ("Bonfire night", "2026-11-05T18:30", "2026-11-05T19:30"),
            "add Sam's wedding 12th December 2026 to my calendar": ("Sam's wedding", "2026-12-12", "2026-12-13"),
            "Add team dinner Oct 3rd 7-9pm to calendar": ("Team dinner", "2026-10-03T19:00", "2026-10-03T21:00"),
            "add standup 5/10 10:30-11:30 to calendar": ("Standup", "2026-10-05T10:30", "2026-10-05T11:30"),
            "add lunch 11-1pm Monday to calendar": ("Lunch", "2026-10-05T11:00", "2026-10-05T13:00"),
            "add call with Bob tomorrow at 10am for 30 minutes to my calendar":
                ("Call with Bob", "2026-09-30T10:00", "2026-09-30T10:30"),
            "add holiday 20/12 all day to my calendar": ("Holiday", "2026-12-20", "2026-12-21"),
            "add parents evening Tuesday 6pm to 7:30pm to calendar":
                ("Parents evening", "2026-10-06T18:00", "2026-10-06T19:30"),  # said on a Tuesday → next week
        }
        for prompt, (title, start, end) in cases.items():
            event = parse_event_request(prompt, now)
            self.assertIsNotNone(event, prompt)
            self.assertEqual((event.title, event.start[:len(start)], event.end[:len(end)]), (title, start, end), prompt)
        self.assertIsNone(parse_event_request("add something to my calendar", now))
        self.assertIsNone(parse_event_request("add MOT 31/02/2027 to calendar", now))  # no such date → model
