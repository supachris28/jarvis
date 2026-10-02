"""Renewals, deadlines and replies you're waiting for — found in email by script."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from jarvis.google.gmail import parse_message
from jarvis.pipelines.deadlines import find_deadline, own_text
from test_jarvis import FakeGmail, IntegrationBase, gmail_message

TODAY = date(2026, 10, 2)


class DeadlineParsing(IntegrationBase.__mro__[1]):
    def test_kinds_and_dates(self):
        cases = [
            ("Your car insurance renewal", "Your car insurance policy with Admiral renews on 16 October 2026. "
             "If you do nothing it will auto-renew.", "renewal", date(2026, 10, 16)),
            ("Your MOT is due soon", "Reminder: the MOT for AB12 CDE expires on 03/11/2026.", "mot", date(2026, 11, 3)),
            ("Your return", "You can return items by 30 Oct 2026 for a full refund.", "return", date(2026, 10, 30)),
            ("Welcome to Netflix", "Your free trial ends on 9 October. You won't be charged before then.", "trial",
             date(2026, 10, 9)),
            ("Your bill is ready", "Your bill of £42.10 is due on 20/10/2026 and will be collected by direct debit.",
             "payment", date(2026, 10, 20)),
            ("Membership", "Your National Trust membership expires on 1 January 2027 — renew to keep visiting.",
             "renewal", date(2027, 1, 1)),
        ]
        for subject, body, kind, due in cases:
            found = find_deadline(subject, body, TODAY)
            self.assertIsNotNone(found, subject)
            self.assertEqual(found[:2], (kind, due), subject)
        self.assertIsNone(find_deadline("Lunch?", "Shall we meet on 14/10 for lunch?", TODAY))
        self.assertIsNone(find_deadline("Renewal", "Your policy renewed on 1 June 2026.", TODAY), "past dates")
        self.assertIsNone(find_deadline("Insurance", "Your policy renews soon — we'll be in touch.", TODAY), "no date")

    def test_own_text_ignores_the_quoted_conversation(self):
        body = "Thanks, that's great.\n\nOn Mon, 28 Sep 2026 at 10:00, Bob <bob@x.com> wrote:\n> Can you send it?\n"
        self.assertEqual(own_text(body).strip(), "Thanks, that's great.")
        self.assertIn("Could you", own_text("Could you check the boiler?\n> old stuff"))


class DeadlineIntegration(IntegrationBase):
    def test_deadlines_and_waiting_on_replies(self):
        s = self.services
        tz = self.settings.tz
        soon = datetime.now(tz).date() + timedelta(days=10)
        later = datetime.now(tz).date() + timedelta(days=60)
        me = "Chris <chris@example.com>"
        sent = gmail_message("s1", "t-boiler", me, "Boiler service", "Hi Bob, could you come and look at the "
                             "boiler next week?\n\nChris", labels=["SENT"], ts=time.time() - 5 * 86400)
        sent["payload"]["headers"][1]["value"] = "Bob Builder <bob@example.com>"
        answered = gmail_message("s2", "t-done", me, "Dinner", "Are you free on Saturday?", labels=["SENT"],
                                 ts=time.time() - 6 * 86400)
        answered["payload"]["headers"][1]["value"] = "Sam Jones <sam@example.com>"
        reply = gmail_message("r2", "t-done", "Sam Jones <sam@example.com>", "Re: Dinner", "Yes!",
                              ts=time.time() - 5 * 86400)
        statement = gmail_message("s3", "t-info", me, "Photos", "Here are the photos from Sunday.", labels=["SENT"],
                                  ts=time.time() - 5 * 86400)
        statement["payload"]["headers"][1]["value"] = "Sam Jones <sam@example.com>"
        insurance = gmail_message(
            "m1", "t-ins", "Admiral <noreply@admiral.com>", "Your home insurance renewal",
            f"Your home insurance policy renews on {soon.day} {soon:%B %Y}. Your new price is £312.",
            labels=["INBOX", "CATEGORY_UPDATES"])
        mot = gmail_message("m2", "t-mot", "DVSA <noreply@dvsa.gov.uk>", "MOT reminder",
                            f"The MOT for your car expires on {later:%d/%m/%Y}.", labels=["INBOX", "CATEGORY_UPDATES"])
        sale = gmail_message("m3", "t-sale", "Shop <news@shop.com>", "Sale — 20% off renewals",
                             f"Renew your membership before {soon:%d/%m/%Y}", labels=["INBOX", "CATEGORY_PROMOTIONS"])
        s.gmail_pipeline.gmail = FakeGmail([sent, answered, reply, statement, insurance, mot, sale])
        self.run_async(s.gmail_pipeline.run())  # first run = backfill, which also checks every email
        items = s.deadlines.upcoming()
        self.assertEqual([(d["kind"], d["due"]) for d in items], [("renewal", soon.isoformat()), ("mot", later.isoformat())])
        self.assertEqual(items[0]["org"], "Admiral")
        self.assertEqual(items[0]["days"], 10)
        # the same renewal mentioned again isn't added twice
        self.assertIsNone(s.deadlines.on_message(parse_message(insurance, 8000)))

        waiting = s.deadlines.waiting()
        self.assertEqual([(w["thread_id"], w["to"]) for w in waiting], [("t-boiler", "Bob Builder")])
        self.assertEqual(waiting[0]["days"], 5)

        # the hourly job: reminds about the renewal (10 days away, inside the 14-day notice), not yet the MOT
        s.db.set("deadlines.looked_back", True)  # no Gmail look-back in this test
        result = self.run_async(s.deadlines.run())
        self.assertEqual(result["reminded"], 1)
        self.assertTrue(s.db.one("SELECT 1 FROM notifications WHERE title LIKE 'Renewal %'"))
        self.assertEqual(self.run_async(s.deadlines.run())["reminded"], 0, "once only")
        self.assertEqual(result["waiting_nudged"], 1)  # logged (followups are off by default → status 'off')

        brief = self.run_async(s.brief.build(with_opener=False))
        self.assertIn("**Renewals and deadlines**", brief)
        self.assertIn("**Waiting on a reply**", brief)
        self.assertIn("Bob Builder — [Boiler service](/#email?thread=t-boiler)", brief)

        async def ask(text):
            events = [e async for e in s.assistant.handle(text)]
            return "".join(e.get("text", "") for e in events if e["type"] == "token")
        self.assertIn("Admiral", self.run_async(ask("Any renewals coming up?")))
        self.assertIn("Bob Builder", self.run_async(ask("Who hasn't replied to me?")))

        # done / not waiting
        s.deadlines.set_status(items[0]["id"], "done")
        self.assertEqual(len(s.deadlines.upcoming()), 1)
        s.deadlines.dismiss_waiting("t-boiler")
        self.assertEqual(s.deadlines.waiting(), [])
