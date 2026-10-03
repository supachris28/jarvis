"""Delivery tracking: found in email, followed on tracking pages, answered in chat."""

from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

from fakes import Server
from jarvis.google.gmail import parse_message
from jarvis.pipelines.deliveries import classify, parcel_jsonld, tracking_link
from test_jarvis import FakeGmail, IntegrationBase, b64, gmail_message

PARCEL_JSONLD = """<script type="application/ld+json">{"@context":"http://schema.org","@type":"ParcelDelivery",
"trackingNumber":"TBA123456789012","trackingUrl":"%s/track/TBA123456789012","expectedArrivalUntil":"2030-01-10T21:00:00Z",
"carrier":{"@type":"Organization","name":"Amazon Logistics"},"itemShipped":{"@type":"Product","name":"Kettle"},
"partOfOrder":{"@type":"Order","orderNumber":"203-1234567-7654321","merchant":{"@type":"Organization","name":"Amazon"},
"orderStatus":"http://schema.org/OrderInTransit"}}</script>"""


class CarrierPages:
    """A fake carrier site: a normal page whose status changes, a JavaScript-only page, and one with JSON data."""

    def __init__(self) -> None:
        self.status = "Your parcel is in transit and on its way to your local depot."
        self.hits = 0

    def app(self) -> Starlette:
        async def track(request):
            self.hits += 1
            return HTMLResponse(f"<html><title>Track</title><body><main><h1>Parcel {request.path_params['n']}</h1>"
                                f"<p>{self.status}</p><p>Estimated delivery: Saturday 3 October</p>"
                                f"<nav>Help · Contact us · Cookies</nav></main></body></html>")

        async def spa(request):
            return HTMLResponse("<html><body><div id='root'></div><script src='/app.js'></script></body></html>")

        async def jsondata(request):
            return HTMLResponse('<html><body><div id="app"></div><script>window.__DATA__ = {"parcel": '
                                '{"currentStatus": "Out for delivery with your driver today"}}</script></body></html>')
        return Starlette(routes=[Route("/track/{n}", track), Route("/spa/{n}", spa), Route("/json/{n}", jsondata)])


class DeliveryTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.settings.web_allow_private = True  # the fake carrier lives on 127.0.0.1
        self.carrier = CarrierPages()
        self.carrier_server = Server(self.carrier.app()).__enter__()
        self.base = self.carrier_server.url

    def tearDown(self):
        self.carrier_server.__exit__()
        super().tearDown()

    def email(self, mid, sender, subject, body, html="<p>ignored</p>", ts=None):
        message = gmail_message(mid, "t" + mid, sender, subject, body, labels=["INBOX", "CATEGORY_UPDATES"], ts=ts)
        message["payload"]["parts"][1]["body"]["data"] = b64(html)
        return message

    def test_script_reading(self):
        self.assertEqual(classify("Good news — your order has been dispatched and will be delivered Friday.")[0], "dispatched")
        self.assertEqual(classify("Your item has arrived at the delivery office")[0], "in_transit")
        self.assertEqual(classify("It was delivered to your porch at 13:02")[0], "delivered")
        self.assertEqual(classify("Thanks for the lovely dinner")[0], "")
        html = ('<a href="https://click.shop.com/abc">View order</a> '
                '<a href="https://www.royalmail.com/track-your-item#/tracking-results/AB123456789GB">Track your parcel</a>')
        self.assertIn("royalmail.com/track-your-item", tracking_link(html, ""))
        data = parcel_jsonld(PARCEL_JSONLD % "https://amazon.co.uk")
        self.assertEqual((data["item"], data["retailer"], data["status"], data["expected"]),
                         ("Kettle", "Amazon", "in_transit", "2030-01-10"))

    def test_emails_then_hourly_checks(self):
        s = self.services
        d = s.deliveries
        now = time.time()
        dispatched = self.email("d1", "Argos <noreply@argos.co.uk>", "Your Argos order has been dispatched",
                                "Your order AR123456 is on its way. Track your parcel with the link below. "
                                "Estimated delivery: Saturday 3 October.",
                                html=f'<a href="{self.base}/track/AB123456789GB">Track your parcel</a>', ts=now - 7200)
        promo = self.email("p1", "Shop <deals@shop.com>", "Free delivery this weekend!", "20% off and free delivery.")
        s.gmail_pipeline.gmail = FakeGmail([dispatched, promo])
        s.db.set("gmail.history_id", "1")
        s.gmail_pipeline.gmail.history = self._history(["d1", "p1"])
        self.run_async(s.gmail_pipeline.run())
        items = d.active()
        self.assertEqual(len(items), 1, items)  # the offer isn't a delivery
        parcel = items[0]
        self.assertEqual((parcel["retailer"], parcel["status"], parcel["tracking_url"]),
                         ("Argos", "dispatched", f"{self.base}/track/AB123456789GB"))
        self.assertEqual(parcel["tracking_number"], "AB123456789GB")
        self.assertTrue(parcel["expected"].endswith("-10-03"))
        self.assertEqual(s.db.one("SELECT COUNT(*) n FROM notifications WHERE title LIKE '%Argos%'")["n"], 1)
        # hourly: the tracking page moved on → updated and notified
        self.assertEqual(self.run_async(d.run())["changed"], 1)
        self.assertEqual(d.get(parcel["id"])["status"], "in_transit")
        self.assertEqual(self.run_async(d.run())["checked"], 0)  # not again within the hour
        s.db.execute("UPDATE deliveries SET last_checked = ?", (now - 4000,))
        self.carrier.status = "Your parcel is out for delivery today."
        self.assertEqual(self.run_async(d.run())["changed"], 1)
        note = s.db.one("SELECT * FROM notifications ORDER BY id DESC")
        self.assertEqual(note["title"], "🛵 Argos: Out for delivery")
        # a later email for the same tracking number updates the same delivery
        delivered = self.email("d2", "Royal Mail <no-reply@royalmail.com>", "Your parcel has been delivered",
                               "Your parcel AB123456789GB was delivered to your safe place at 14:02.")
        s.gmail_pipeline.gmail = FakeGmail([delivered])
        s.gmail_pipeline.gmail.history = self._history(["d2"])
        self.run_async(s.gmail_pipeline.run())
        item = d.get(parcel["id"])
        self.assertEqual(item["status"], "delivered")
        self.assertEqual([h["via"] for h in item["history"]], ["email", "page", "page", "email"])
        # a lagging tracking page can't un-deliver it, and delivered parcels aren't polled
        self.carrier.status = "Your parcel is in transit."
        s.db.execute("UPDATE deliveries SET last_checked = ?", (now - 4000,))
        self.assertEqual(self.run_async(d.run())["checked"], 0)
        # gone from the list three days after delivery
        s.db.execute("UPDATE deliveries SET updated = ?", (now - 4 * 86400,))
        self.run_async(d.run())
        self.assertEqual(d.active(), [])

    def test_structured_email_and_pages_that_need_a_browser(self):
        s = self.services
        d = s.deliveries
        amazon = parse_message(self.email("a1", "Amazon.co.uk <shipment-tracking@amazon.co.uk>",
                                          "Your Amazon.co.uk order has dispatched", "Your package is on the way.",
                                          html=PARCEL_JSONLD % self.base))
        delivery_id = d.on_message(amazon)
        item = d.get(delivery_id)
        self.assertEqual((item["name"], item["retailer"], item["status"], item["order_number"]),
                         ("Kettle", "Amazon", "in_transit", "203-1234567-7654321"))
        # status found in data the page embeds for its JavaScript
        s.db.execute("UPDATE deliveries SET tracking_url = ? WHERE id = ?", (f"{self.base}/json/X1", delivery_id))
        self.assertEqual(self.run_async(d.check(delivery_id))["status"], "out_for_delivery")
        # a page that only renders in a browser: three tries, then follow emails instead
        spa = d.add_from_chat(f"track {self.base}/spa/ZZ99887766")
        for _ in range(3):
            result = self.run_async(d.check(spa["id"]))
        self.assertTrue(result["stopped"])
        self.assertEqual(d.get(spa["id"])["poll"], 0)
        self.assertIn("needs a full browser", d.get(spa["id"])["poll_note"])

    def test_look_back_over_older_email(self):
        s = self.services
        now = time.time()
        old = [  # given newest first, as Gmail does — read oldest first so the status ends up right
            self.email("o3", "Royal Mail <no-reply@royalmail.com>", "Your parcel is out for delivery",
                       "Your parcel AB111111111GB is out for delivery today.", ts=now - 3600),
            self.email("o2", "Hobbycraft <orders@hobbycraft.co.uk>", "Your order has been dispatched",
                       "Tracking number: AB111111111GB. Estimated delivery: tomorrow.", ts=now - 86400),
            self.email("o1", "Argos <noreply@argos.co.uk>", "Your parcel has been delivered",
                       "Your parcel AB222222222GB was delivered to your porch.", ts=now - 10 * 86400),
            self.email("o0", "Sam Jones <sam@example.com>", "Dinner", "Delivered the cake to your mum's, all good."),
        ]
        gmail = FakeGmail(old)
        asked = []

        async def list_ids(query, limit=500):
            asked.append(query)
            return [m["id"] for m in old]
        gmail.list_message_ids = list_ids
        s.deliveries.gmail = gmail
        result = self.run_async(s.deliveries.look_back(30))
        self.assertIn("newer_than:30d", asked[0])
        active = s.deliveries.active()
        self.assertEqual([(d["tracking_number"], d["status"]) for d in active], [("AB111111111GB", "out_for_delivery")])
        self.assertEqual(active[0]["retailer"], "Hobbycraft")  # the retailer from the dispatch email is kept
        self.assertEqual(result["new"], 1)  # the long-delivered Argos parcel is tidied away, not listed
        self.assertEqual(s.db.one("SELECT COUNT(*) n FROM notifications")["n"], 0)  # nothing announced
        # in chat
        events = self.run_async(self._ask("look back through my emails for deliveries"))
        self.assertIn("found 0 parcel(s) I wasn't tracking yet", "".join(e.get("text", "") for e in events))

    async def _collect(self, generator):
        return [e async for e in generator]

    def _ask(self, text):
        return self._collect(self.services.assistant.handle(text))

    def test_item_names_for_new_and_older_deliveries(self):
        s = self.services
        d = s.deliveries
        argos = parse_message(self.email("i1", "Argos <noreply@argos.co.uk>", "Your Argos order has been dispatched",
                                         "Your order AR555 is on its way. Tracking number: AB333333333GB\n\n"
                                         "Tefal Ultimate Iron\nQty: 1\n£34.99\n\nDelivery £3.95"))
        item = d.get(d.on_message(argos))
        self.assertEqual((item["name"], item["retailer"]), ("Tefal Ultimate Iron", "Argos"))
        amazon = parse_message(self.email("i2", "Amazon.co.uk <shipment-tracking@amazon.co.uk>",
                                          'Dispatched: "Philips HD9350 Kettle" and 2 more items',
                                          "Your package is on the way. Track your package: https://amazon.co.uk/progress-tracker/x"))
        self.assertEqual(d.get(d.on_message(amazon))["item"], "Philips HD9350 Kettle +2 more")
        # a carrier's email names the shop it's from, not the carrier, as the retailer
        rm = parse_message(self.email("i3", "Royal Mail <no-reply@royalmail.com>", "Your parcel is on its way",
                                      "Your parcel from Hobbycraft is on its way. Tracking number AB444444444GB."))
        self.assertEqual(d.get(d.on_message(rm))["retailer"], "Hobbycraft")
        # a delivery recorded before items were read: its stored email is re-read
        s.db.execute("INSERT INTO emails (message_id, thread_id, ts, from_addr, from_name, to_addrs, subject, labels, bulk, "
                     "outgoing, snippet, body, attachments) VALUES ('old1', 'told1', 1, 'x@lakeland.co.uk', 'Lakeland', "
                     "'[]', 'Your order is on its way', '[]', 1, 0, '', ?, '[]')",
                     ("Your order\nLakeland Dehumidifier 12L   £149.99\nDelivery   £0.00",))
        s.db.execute("INSERT INTO deliveries (created, updated, retailer, status, thread_id, history) "
                     "VALUES (1, ?, 'Lakeland', 'dispatched', 'told1', '[]')", (time.time(),))
        self.assertEqual(self.run_async(d.fill_items()), 1)
        self.assertEqual(s.db.one("SELECT item FROM deliveries WHERE thread_id = 'told1'")["item"],
                         "Lakeland Dehumidifier 12L")
        self.assertEqual(self.run_async(d.fill_items()), 0)  # each one is only tried once

    def test_progress_graphics_and_delivery_time(self):
        from datetime import datetime as dt
        from pathlib import Path
        s = self.services
        d = s.deliveries
        tracker = (Path(__file__).parent / "fixtures" / "amazon_step_tracker.html").read_text()
        body = ("Your Account\nhttps://www.amazon.co.uk/your-account\n\nYour package was dispatched!\n\n"
                "Ordered\nDispatched\nOut for delivery\nDelivered\nArriving tomorrow")
        amazon = parse_message(self.email("z1", "Amazon.co.uk <shipment-tracking@amazon.co.uk>",
                                          'Dispatched: "Bamboo chopping board"', body, html=tracker))
        item = d.get(d.on_message(amazon))
        self.assertEqual(item["status"], "dispatched")  # the graphic lists "Delivered", but it isn't ticked
        self.assertIn("progress tracker", item["status_text"])
        # when it was delivered: the time in the email, on the email's day
        sent = dt.now(s.settings.tz).replace(hour=18, minute=0, second=0, microsecond=0).timestamp()
        done = parse_message(self.email("z2", "Royal Mail <no-reply@royalmail.com>", "Your parcel has been delivered",
                                        "Your parcel AB555555555GB was delivered to your safe place at 14:02.", ts=sent))
        item = d.get(d.on_message(done))
        self.assertEqual(item["delivered_text"], "today at 14:02")
        self.assertEqual(item["label"], "Delivered today")
        self.assertIn("Delivered today at 14:02", d.summary_lines()[0] + "".join(d.summary_lines()))
        # parcels wrongly marked delivered by older versions are corrected from their emails
        s.db.execute("UPDATE deliveries SET status = 'delivered', status_text = 'Delivered', active = 0 WHERE thread_id = 'tz1'")

        class Thread:
            async def thread_messages(self, thread_id):
                return [self_email] if thread_id == "tz1" else []
        self_email = self.email("z1", "Amazon.co.uk <shipment-tracking@amazon.co.uk>", 'Dispatched: "Bamboo chopping board"',
                                body, html=tracker)
        d.gmail = Thread()
        self.assertEqual(self.run_async(d.recheck_delivered()), 1)
        fixed = s.db.one("SELECT * FROM deliveries WHERE thread_id = 'tz1'")
        self.assertEqual((fixed["status"], fixed["active"], fixed["delivered_at"]), ("dispatched", 1, None))

    def test_chat(self):
        s = self.services

        async def ask(text):
            return [e async for e in s.assistant.handle(text)]

        def said(events):
            return "".join(e.get("text", "") for e in events if e["type"] == "token")
        self.assertIn("No deliveries", said(self.run_async(ask("Any deliveries today?"))))
        events = self.run_async(ask(f"track my new boots {self.base}/track/QQ123456789GB"))
        self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "deliveries")
        self.assertIn("**On its way**", said(events))
        self.assertIn("every hour", said(events))
        text = said(self.run_async(ask("Where's my parcel?")))
        self.assertIn("On its way", text)
        self.assertIn("new boots", text)
        text = said(self.run_async(ask("track parcel AB987654321GB")))
        self.assertIn("Royal Mail", text)
        self.assertEqual(s.deliveries.get(2)["tracking_url"],
                         "https://www.royalmail.com/track-your-item#/tracking-results/AB987654321GB")

    @staticmethod
    def _history(ids):
        async def history(start):
            return ids, "2"
        return history


class CollectionTests(IntegrationBase):
    def test_ready_to_collect(self):
        from datetime import datetime, timedelta
        s = self.services
        n = s.notifier
        self.settings.notify_quiet_hours = "00:00-00:01"  # not quiet now
        tz = self.settings.tz
        tomorrow = datetime.now(tz).date() + timedelta(days=1)
        dispatched = gmail_message("c1", "tc1", "InPost <noreply@inpost.co.uk>", "Your parcel is on its way",
                                   "Your parcel from Hobbycraft has been dispatched. Tracking number: 6912345678901234567890",
                                   ts=time.time() - 86400)
        ready = gmail_message(
            "c2", "tc1", "InPost <noreply@inpost.co.uk>", "Your parcel is ready to collect",
            "Good news! Your parcel from Hobbycraft has been delivered to the locker and is ready to collect from "
            "the InPost Locker at Tesco Extra, Hagley Road. Your collection code is 482915. "
            f"Collect it by {tomorrow:%d/%m/%Y}.", ts=time.time() - 60)
        click = gmail_message("c3", "tc3", "Boots <orders@boots.com>", "Your Click & Collect order is ready",
                              "Your Click & Collect order is ready.\nCollect from: Boots, New Street, Birmingham\n"
                              "Please bring your order number 12345678.")
        for message in (dispatched, ready, click):
            s.deliveries.on_message(parse_message(message, 8000))
        items = {d["thread_id"]: d for d in s.deliveries.active()}
        locker = items["tc1"]
        self.assertEqual((locker["status"], locker["label"], locker["icon"]), ("ready_to_collect", "Ready to collect", "📍"))
        self.assertEqual((locker["collect_place"], locker["collect_code"], locker["collect_by_text"]),
                         ("InPost Locker at Tesco Extra, Hagley Road", "482915", "tomorrow"))
        self.assertEqual(items["tc3"]["status"], "ready_to_collect")
        self.assertEqual(items["tc3"]["collect_place"], "Boots, New Street, Birmingham")
        # its own notification category, posted in the chat too (chat is on by default for collections)
        self.assertTrue(n.preferences()["collections"]["chat"])
        self.run_async(s.deliveries.flush_notifications())
        row = s.db.one("SELECT title, message FROM notifications WHERE title LIKE '📍 Ready to collect%' "
                       "AND message LIKE '%482915%'")
        self.assertEqual(row["message"], "At InPost Locker at Tesco Extra, Hagley Road · code 482915 · collect by tomorrow")
        self.assertTrue(s.db.one("SELECT 1 FROM chat_messages WHERE role = 'activity' AND content LIKE '%Ready to collect%'"))
        # last-day reminder (collect by tomorrow), once
        if datetime.now(tz).hour >= 9:
            self.assertEqual(self.run_async(s.deliveries.collect_reminders()), 1)
            self.assertEqual(self.run_async(s.deliveries.collect_reminders()), 0)
        # switching the category's chat tick off keeps it out of the chat
        n.set_preferences({"collections": {"chat": False}})
        self.assertFalse(n.in_chat("collections"))
        # listed in the brief and answered in chat
        brief = self.run_async(s.brief.build(with_opener=False))
        self.assertIn("**Ready to collect**", brief)
        self.assertIn("code 482915", brief)

        async def ask(text):
            return "".join(e.get("text", "") for e in [e async for e in s.assistant.handle(text)] if e["type"] == "token")
        answer = self.run_async(ask("Anything to collect?"))
        self.assertIn("InPost Locker", answer)
        self.assertIn("Boots, New Street", answer)
        # collected → done
        done = gmail_message("c4", "tc1", "InPost <noreply@inpost.co.uk>", "Thanks for collecting",
                             "Thanks for collecting your parcel from the locker. Tracking number: 6912345678901234567890")
        s.deliveries.on_message(parse_message(done, 8000))
        collected = s.deliveries.get(locker["id"])
        self.assertEqual((collected["status"], collected["label"].split()[0]), ("delivered", "Collected"))
        self.assertEqual(classify("We'll email you when it's ready to collect.")[0], "")
