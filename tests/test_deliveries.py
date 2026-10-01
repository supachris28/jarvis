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
