"""Delivery tracking — scripts only (Tier 0), no AI.

Deliveries are found in email (dispatch / "out for delivery" / "delivered" messages, tracking links and numbers,
and schema.org ParcelDelivery data that many retailers embed) or added in chat ("track <link or number>").
Each active delivery's tracking page is followed hourly; its status is read by script from the page text and from
status fields in embedded JSON. When a page only works in a full browser (many carriers build theirs with
JavaScript) polling stops and the delivery follows your emails instead.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time

import httpx
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from .. import diag
from ..config import Settings
from ..db import Database
from ..extract.event_text import _find_date
from ..notify import Notifier
from ..vault.markdown import one_line
from ..websearch import WebError, fetch_page, page_text

# ---------------------------------------------------------------------------- carriers
CARRIERS: list[dict] = [
    {"name": "Royal Mail", "domains": ("royalmail.com", "royalmail.co.uk"), "number": r"\b[A-Z]{2}\d{9}GB\b",
     "url": "https://www.royalmail.com/track-your-item#/tracking-results/{n}"},
    {"name": "Parcelforce", "domains": ("parcelforce.com",), "number": "",
     "url": "https://www.parcelforce.com/track-trace?trackNumber={n}"},
    {"name": "UPS", "domains": ("ups.com",), "number": r"\b1Z[0-9A-Z]{16}\b", "url": "https://www.ups.com/track?tracknum={n}"},
    {"name": "Yodel", "domains": ("yodel.co.uk",), "number": r"\bJD\d{16,18}\b",
     "url": "https://www.yodel.co.uk/tracking/{n}"},
    {"name": "Evri", "domains": ("evri.com", "myhermes.co.uk", "hermes-europe.co.uk"), "number": r"\bH[0-9A-Z]{15}\b",
     "url": "https://www.evri.com/track/parcel/{n}"},
    {"name": "DPD", "domains": ("dpd.co.uk", "dpdlocal.co.uk", "dpd.com"), "number": r"\b1550\d{10}\b",
     "url": "https://track.dpd.co.uk/parcels/{n}"},
    {"name": "DHL", "domains": ("dhl.co.uk", "dhl.com", "dhlparcel.co.uk"), "number": "", "url": ""},
    {"name": "FedEx", "domains": ("fedex.com",), "number": "", "url": "https://www.fedex.com/fedextrack/?trknbr={n}"},
    {"name": "InPost", "domains": ("inpost.co.uk",), "number": "", "url": ""},
    {"name": "Amazon", "domains": ("amazon.co.uk", "amazon.com", "amzn.to", "amzn.eu"), "number": r"\bTBA\d{12}\b", "url": ""},
    {"name": "Royal Mail", "domains": ("rmg.co.uk",), "number": "", "url": ""},
]
GENERIC_NUMBER = re.compile(r"(?:tracking|parcel|consignment|shipment)\s*(?:number|no\.?|ref(?:erence)?|id|code)\s*"
                            r"(?:is|:|#)?\s*([A-Z0-9][A-Z0-9-]{7,29})\b", re.I)
ORDER_NUMBER = re.compile(r"\border\s*(?:number|no\.?|ref(?:erence)?|#|id)\s*(?:is|:|#)?\s*([A-Z0-9][A-Z0-9-]{4,29})\b", re.I)
TRACK_LINK_TEXT = re.compile(r"\btrack(?:ing)?\b|\bwhere'?s my\b|\bfollow (?:your|my) (?:parcel|order|delivery)\b", re.I)

# ---------------------------------------------------------------------------- status by script
STATUSES = ["ordered", "dispatched", "in_transit", "out_for_delivery", "attempted", "delayed", "delivered"]
LABELS = {"ordered": "Ordered", "dispatched": "Dispatched", "in_transit": "On its way", "out_for_delivery":
          "Out for delivery", "attempted": "Delivery attempted", "delayed": "Delayed", "delivered": "Delivered"}
ICONS = {"ordered": "🧾", "dispatched": "📦", "in_transit": "🚚", "out_for_delivery": "🛵", "attempted": "📭",
         "delayed": "⏳", "delivered": "✅"}
# checked in this order: the first match wins, so "delivered" beats "out for delivery" in the same text
STATUS_PATTERNS = [
    ("delivered", re.compile(r"\b(?:has been|have been|was|were|been|successfully|now)\s+delivered\b|"
                             r"\bdelivered\s+(?:to|at|into|in)\s+(?:your|the|a|safe|front|back|porch|neighbour|parcel|mailbox|letterbox)|"
                             r"\b(?:parcel|order|package|item)s?\s+(?:has|have)\s+arrived\b(?!\s+(?:at|in|into|with))|^\s*delivered\b|\bstatus:?\s*delivered\b",
                             re.I | re.M)),
    ("attempted", re.compile(r"\b(?:sorry we missed you|we missed you|attempted (?:to deliver|delivery)|delivery attempt(?:ed)?|"
                             r"we (?:tried|were unable) to deliver|card (?:was )?left|no one (?:was )?(?:home|available))\b", re.I)),
    ("delayed", re.compile(r"\b(?:delayed|is running late|running behind|delivery exception|held (?:at|by) customs|"
                           r"unable to deliver|on hold|rescheduled)\b", re.I)),
    ("out_for_delivery", re.compile(r"\b(?:out for delivery|with (?:the|your|our) (?:driver|courier) (?:today|now)|"
                                    r"(?:arriving|being delivered|will be delivered|delivering) today|on (?:the|its) way to you today|"
                                    r"on the van)\b", re.I)),
    ("in_transit", re.compile(r"\b(?:in transit|on the move|at (?:the|our|a|your) (?:local )?(?:depot|hub|delivery office|"
                              r"sorting (?:centre|center|office)|facility)|arrived at|departed|left (?:the|our) (?:warehouse|depot|hub)|"
                              r"received by (?:the )?(?:carrier|courier)|collected (?:by|from) (?:the )?(?:courier|carrier)|"
                              r"with the courier|handed (?:over )?to)\b", re.I)),
    ("dispatched", re.compile(r"\b(?:dispatched|despatched|shipped|has been sent|have been sent|is on (?:the|its) way|"
                              r"ready (?:to ship|for dispatch)|label created|shipping label)\b", re.I)),
    ("ordered", re.compile(r"\b(?:order (?:confirmed|confirmation|received)|thanks for your order|we've received your order)\b", re.I)),
]
SCHEMA_STATUS = {"OrderDelivered": "delivered", "OrderInTransit": "in_transit", "OrderPickupAvailable": "out_for_delivery",
                 "OrderProcessing": "ordered", "OrderProblem": "delayed", "OrderReturned": "delayed",
                 "OrderPaymentDue": "ordered"}
DELIVERY_EMAIL = re.compile(
    r"\b(?:dispatched|despatched|shipped|out for delivery|has been delivered|was delivered|been delivered|"
    r"on (?:its|the) way|in transit|track (?:your|my) (?:order|parcel|package|delivery|item)|tracking (?:number|link|info)|"
    r"delivery (?:update|scheduled|window|slot|date|is (?:due|expected))|your (?:parcel|package|delivery)|"
    r"arriving (?:today|tomorrow|on)|missed you|attempted delivery|courier)\b", re.I)
NOT_DELIVERY = re.compile(r"\b(?:free delivery|delivery (?:charges|pass|offer)|% off|sale|newsletter|"
                          r"subscribe|unsubscribe from deliveries)\b", re.I)
EXPECTED = re.compile(r"\b(?:arriving|arrives|expected(?: delivery)?|estimated(?: delivery)?|delivery date|due|"
                      r"will be delivered|delivered by|get it|should arrive|arrive)\b[^.\n]{0,12}?(?:on|by|between|:)?\s*"
                      r"(?P<rest>[^\n]{0,60})", re.I)
LOOK_BACK_TERMS = ['dispatched', 'despatched', 'shipped', '"out for delivery"', '"on its way"', 'delivered',
                   '"tracking number"', '"track your"', '"your parcel"', '"your package"', '"missed you"', 'courier']
JSON_STATUS = re.compile(r'"(?:status|statusDescription|statusText|state|description|eventDescription|summary|'
                         r'trackingStatus|deliveryStatus|currentStatus)"\s*:\s*"([^"]{3,120})"', re.I)


def classify(text: str) -> tuple[str, str]:
    """(status, the sentence that shows it) — or ('', '') when the text says nothing about a delivery."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    for status, pattern in STATUS_PATTERNS:
        for sentence in sentences:
            if not pattern.search(sentence):
                continue
            if status == "delivered" and FUTURE.search(sentence):
                continue  # "will be delivered", "once it's delivered" — not delivered yet
            return status, one_line(sentence, 160)
    return "", ""


FUTURE = re.compile(r"\b(?:will|would|should|could|to be|once|when|if|before|after it(?:'s| is))\b[^.]{0,40}\bdelivered\b",
                    re.I)


def find_expected(text: str, today: date) -> str:
    for match in EXPECTED.finditer(text):
        rest = match.group("rest")
        if re.match(r"\s*today\b", rest, re.I):
            return today.isoformat()
        if re.match(r"\s*tomorrow\b", rest, re.I):
            return (today + timedelta(days=1)).isoformat()
        day, _ = _find_date(rest, today)
        if day and today - timedelta(days=7) <= day <= today + timedelta(days=60):
            return day.isoformat()
    return ""


def carrier_for(url: str = "", text: str = "") -> dict | None:
    host = (urlsplit(url).hostname or "").casefold() if url else ""
    for carrier in CARRIERS:
        if host and any(host == d or host.endswith("." + d) for d in carrier["domains"]):
            return carrier
    for carrier in CARRIERS:
        if carrier["number"] and re.search(carrier["number"], text):
            return carrier
    return None


def tracking_number(text: str) -> tuple[str, dict | None]:
    for carrier in CARRIERS:
        if carrier["number"]:
            match = re.search(carrier["number"], text)
            if match:
                return match.group(0), carrier
    match = GENERIC_NUMBER.search(text)
    if match and re.search(r"\d", match.group(1)):
        return match.group(1).upper(), None
    return "", None


def links(markup: str) -> list[tuple[str, str]]:
    """(href, link text) pairs from an HTML email."""
    found = []
    for match in re.finditer(r"(?is)<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", markup or ""):
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", match.group(2)))
        found.append((htmllib.unescape(match.group(1)).strip(), re.sub(r"\s+", " ", text).strip()))
    return found


def tracking_link(markup: str, body: str) -> str:
    """The best tracking link in an email: a carrier's own site first, then any link labelled 'track…'."""
    pairs = links(markup)
    pairs += [(u, "") for u in re.findall(r"https?://[^\s<>\"')\]]+", body or "")]
    candidates = [(href, text) for href, text in pairs if href.startswith(("http://", "https://"))]
    for href, _ in candidates:
        if carrier_for(href) and re.search(r"track|trace|parcel|consignment|shipment|progress", href, re.I):
            return href
    for href, text in candidates:
        if TRACK_LINK_TEXT.search(text) or re.search(r"/track|tracking|trackandtrace|track-trace|progress-tracker", href, re.I):
            return href
    return ""


def parcel_jsonld(markup: str) -> dict:
    """schema.org ParcelDelivery embedded by many retailers (Amazon, Argos, eBay…)."""
    for block in re.findall(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', markup or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or "ParcelDelivery" not in str(item.get("@type", "")):
                continue
            order = item.get("partOfOrder") or {}
            merchant = (order.get("merchant") or {}) if isinstance(order, dict) else {}
            shipped = item.get("itemShipped") or {}
            status = str(item.get("deliveryStatus") or (order.get("orderStatus") if isinstance(order, dict) else "") or "")
            carrier = item.get("carrier") or item.get("provider") or {}
            return {
                "tracking_number": str(item.get("trackingNumber") or ""),
                "tracking_url": str(item.get("trackingUrl") or ""),
                "carrier": str(carrier.get("name") if isinstance(carrier, dict) else carrier or ""),
                "item": str(shipped.get("name") if isinstance(shipped, dict) else ""),
                "retailer": str(merchant.get("name") if isinstance(merchant, dict) else ""),
                "order_number": str(order.get("orderNumber") or "") if isinstance(order, dict) else "",
                "expected": str(item.get("expectedArrivalUntil") or item.get("expectedArrivalFrom") or "")[:10],
                "status": SCHEMA_STATUS.get(status.rsplit("/", 1)[-1], ""),
            }
    return {}


class Deliveries:
    def __init__(self, settings: Settings, db: Database, notifier: Notifier) -> None:
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self._pending: list[int] = []
        self.gmail = None  # Gmail client, attached by Services (for looking back over older email)

    # ------------------------------------------------------------------ reading
    def get(self, delivery_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM deliveries WHERE id = ?", (delivery_id,))
        return self.present(row) if row else None

    def present(self, row) -> dict:
        item = dict(row)
        item["history"] = json.loads(item.get("history") or "[]")
        item["label"] = LABELS.get(item["status"], item["status"])
        item["icon"] = ICONS.get(item["status"], "📦")
        item["name"] = item["item"] or item["retailer"] or item["carrier"] or "Parcel"
        tz = self.settings.tz
        item["expected_text"] = ""
        if item["expected"]:
            try:
                day = date.fromisoformat(item["expected"])
                today = datetime.now(tz).date()
                item["expected_text"] = "today" if day == today else "tomorrow" if day == today + timedelta(days=1) \
                    else f"{day:%a %d %b}"
            except ValueError:
                pass
        item["checked_text"] = datetime.fromtimestamp(item["last_checked"], tz).strftime("%a %H:%M") \
            if item["last_checked"] else ""
        return item

    def active(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM deliveries WHERE active = 1 ORDER BY status = 'delivered', "
                           "expected = '', expected, updated DESC")
        return [self.present(r) for r in rows]

    # ------------------------------------------------------------------ from email
    def on_message(self, message) -> int | None:
        """Called for every new email. Returns the delivery id when the email was about one."""
        if "SPAM" in message.labels or "CATEGORY_PROMOTIONS" in message.labels or message.outgoing:
            return None
        text = f"{message.subject}\n{message.body}"
        data = parcel_jsonld(message.html)
        if not data and (not DELIVERY_EMAIL.search(text) or NOT_DELIVERY.search(message.subject)):
            return None
        number, carrier = tracking_number(text)
        number = data.get("tracking_number") or number
        url = data.get("tracking_url") or tracking_link(message.html, message.body)
        if url and not number:  # the number is often only in the link (…/tracking-results/AB123456789GB)
            number, found = tracking_number(url)
            carrier = carrier or found
        carrier = carrier or carrier_for(url, text)
        status, evidence = classify(f"{message.subject}.\n{message.body[:3000]}")
        status = data.get("status") or status
        if not (number or url or data) and status not in {"out_for_delivery", "delivered", "dispatched", "attempted"}:
            return None  # talks about delivery but gives nothing to track (e.g. a marketing mention)
        order = data.get("order_number") or (ORDER_NUMBER.search(text).group(1) if ORDER_NUMBER.search(text) else "")
        now = datetime.now(self.settings.tz)
        fields = {
            "retailer": data.get("retailer") or (message.from_name if not carrier or carrier["name"].casefold()
                                                  not in message.from_name.casefold() else ""),
            "item": data.get("item") or "",
            "carrier": data.get("carrier") or (carrier["name"] if carrier else ""),
            "tracking_number": number,
            "tracking_url": url or (carrier["url"].format(n=number) if carrier and carrier["url"] and number else ""),
            "order_number": order,
            "expected": data.get("expected") or find_expected(text, now.date()),
            "thread_id": message.thread_id,
        }
        return self.upsert(fields, status or "dispatched", evidence or one_line(message.subject, 160),
                           via="email", ts=message.ts, title=message.subject)

    # ------------------------------------------------------------------ storage
    def _match(self, fields: dict):
        for key in ("tracking_number", "tracking_url", "order_number"):
            if fields.get(key):
                row = self.db.one(f"SELECT * FROM deliveries WHERE {key} = ? ORDER BY id DESC", (fields[key],))
                if row:
                    return row
        if fields.get("thread_id"):
            return self.db.one("SELECT * FROM deliveries WHERE thread_id = ? ORDER BY id DESC", (fields["thread_id"],))
        return None

    def upsert(self, fields: dict, status: str, text: str, via: str, ts: float | None = None, title: str = "") -> int:
        ts = ts or time.time()
        row = self._match(fields)
        if row is None:
            history = [{"ts": ts, "status": status, "text": text, "via": via}]
            cursor = self.db.execute(
                "INSERT INTO deliveries (created, updated, retailer, item, carrier, tracking_number, tracking_url, "
                "order_number, status, status_text, expected, source, thread_id, poll, history) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), ts, fields.get("retailer", ""), fields.get("item", ""),
                 fields.get("carrier", ""), fields.get("tracking_number", ""), fields.get("tracking_url", ""),
                 fields.get("order_number", ""), status, text, fields.get("expected", ""), via if via == "chat" else "email",
                 fields.get("thread_id", ""), int(bool(fields.get("tracking_url"))), json.dumps(history)))
            diag.event("deliveries", f"new delivery: {fields.get('retailer') or fields.get('carrier')} — {LABELS[status]}",
                       email=title, **{k: v for k, v in fields.items() if v})
            self._pending.append(cursor.lastrowid)
            return cursor.lastrowid
        # fill in anything we didn't know, keep what we did
        updates = {k: v for k, v in fields.items() if v and not row[k] and k != "thread_id"}
        if fields.get("expected") and fields["expected"] != row["expected"]:
            updates["expected"] = fields["expected"]
        if updates.get("tracking_url") and not row["poll"] and not row["poll_note"]:
            updates["poll"] = 1
        if updates:
            self.db.execute(f"UPDATE deliveries SET {', '.join(k + ' = ?' for k in updates)} WHERE id = ?",
                            (*updates.values(), row["id"]))
        if ts >= row["updated"] - 1:  # older emails processed late never overwrite a newer status
            self._set_status(row, status, text, via, ts)
        return row["id"]

    def _set_status(self, row, status: str, text: str, via: str, ts: float) -> bool:
        if status == row["status"] and text == row["status_text"]:
            return False
        if row["status"] == "delivered" and status != "delivered" and via == "page":
            return False  # a tracking page that lags behind doesn't un-deliver a parcel
        history = json.loads(row["history"] or "[]")
        history.append({"ts": ts, "status": status, "text": text, "via": via})
        self.db.execute("UPDATE deliveries SET status = ?, status_text = ?, updated = ?, history = ? WHERE id = ?",
                        (status, text, ts, json.dumps(history[-30:]), row["id"]))
        if status != row["status"]:
            diag.event("deliveries", f"{row['item'] or row['retailer'] or 'parcel'}: {LABELS.get(row['status'])} → "
                                     f"{LABELS[status]}", via=via, text=text)
            self._pending.append(row["id"])
        return True

    async def flush_notifications(self) -> int:
        ids, self._pending = list(dict.fromkeys(self._pending)), []
        for delivery_id in ids:
            item = self.get(delivery_id)
            if not item:
                continue
            expected = f" · expected {item['expected_text']}" if item["expected_text"] and item["status"] != "delivered" else ""
            await self.notifier.notify(
                f"{item['icon']} {one_line(item['name'], 50)}: {item['label']}",
                f"{item['status_text']}{expected}", 4 if item["status"] in {"out_for_delivery", "attempted"} else 3,
                url=item["tracking_url"] or self.settings.public_url.rstrip("/") + "/#plan",
                dedupe=f"delivery:{delivery_id}:{item['status']}:{int(item['updated'])}", tags="package",
                category="deliveries")
        return len(ids)

    # ------------------------------------------------------------------ from chat
    def add_from_chat(self, text: str) -> dict:
        urls = re.findall(r"https?://[^\s<>\"']+", text)
        number, carrier = tracking_number(text.upper() if not urls else text)
        if not urls and not number:
            bare = re.search(r"\b([A-Z0-9]{8,30})\b", text.upper())
            if bare and re.search(r"\d", bare.group(1)):
                number = bare.group(1)
        url = urls[0] if urls else (carrier["url"].format(n=number) if carrier and carrier["url"] else "")
        carrier = carrier or carrier_for(url, text)
        if not url and not number:
            return {"error": "Send me the tracking link (or the tracking number) and I'll follow it."}
        fields = {"carrier": carrier["name"] if carrier else "", "tracking_number": number, "tracking_url": url,
                  "item": one_line(re.sub(r"https?://\S+|\btrack(?:ing)?\b|\b(?:my|the|this|parcel|package|"
                                          r"delivery|order|number|link|please)\b|" + re.escape(number or "\x00"),
                                          " ", text, flags=re.I), 60)}
        delivery_id = self.upsert(fields, "dispatched", "Added by you.", via="chat")
        return self.get(delivery_id)

    async def look_back(self, days: int = 30, limit: int = 300) -> dict:
        """Find deliveries in email you already have: a Gmail search for delivery-ish messages from the last `days`,
        read oldest first so each parcel's status builds up in order. Nothing is announced; parcels already
        delivered more than three days ago are tidied away by the next hourly run."""
        from ..google.gmail import parse_message
        if self.gmail is None:
            return {"error": "Gmail isn't connected."}
        query = (f"newer_than:{int(days)}d -category:promotions ({' OR '.join(LOOK_BACK_TERMS)})")
        ids = await self.gmail.list_message_ids(query, limit)
        before = {r["id"] for r in self.db.all("SELECT id FROM deliveries")}
        messages = []
        for message_id in ids:
            try:
                messages.append(parse_message(await self.gmail.message(message_id), self.settings.email_body_limit))
            except Exception as error:  # noqa: BLE001 — one unreadable email doesn't stop the look-back
                diag.debug("deliveries", f"skipped an email in the look-back: {type(error).__name__}", id=message_id)
        found = 0
        for message in sorted(messages, key=lambda m: m.ts):
            if self.on_message(message) is not None:
                found += 1
        self._pending.clear()
        self.db.execute("UPDATE deliveries SET active = 0 WHERE active = 1 AND status = 'delivered' AND updated < ?",
                        (time.time() - 3 * 86400,))
        new = [r for r in self.active() if r["id"] not in before]
        self.db.set("deliveries.looked_back", time.time())
        diag.event("deliveries", f"looked back {days} days: {len(ids)} candidate email(s), {found} about parcels, "
                                 f"{len(new)} new active deliveries", query=query)
        return {"emails": len(ids), "about_parcels": found, "new": len(new), "active": len(self.active())}

    def quiet(self, delivery_id: int) -> None:
        """Don't notify about changes you've just been shown (a delivery added and checked from chat)."""
        self._pending = [i for i in self._pending if i != delivery_id]

    def archive(self, delivery_id: int) -> None:
        self.db.execute("UPDATE deliveries SET active = 0 WHERE id = ?", (delivery_id,))

    # ------------------------------------------------------------------ hourly check
    async def check(self, delivery_id: int) -> dict:
        row = self.db.one("SELECT * FROM deliveries WHERE id = ?", (delivery_id,))
        if row is None or not row["tracking_url"]:
            return {"ok": False, "detail": "no tracking link"}
        now = time.time()
        try:
            final, markup = await fetch_page(row["tracking_url"], self.settings.web_allow_private, timeout=15)
        except (WebError, httpx.HTTPError) as error:
            return self._check_failed(row, f"couldn't open the tracking page ({type(error).__name__}: {error})")
        _, text = page_text(markup)
        status, evidence = classify(text)
        if not status:  # data embedded for the page's JavaScript often holds the status
            for value in JSON_STATUS.findall(markup):
                status, evidence = classify(value)
                if status:
                    break
        if not status:
            return self._check_failed(row, "the tracking page didn't show a status (it may need a full browser)")
        expected = find_expected(text, datetime.now(self.settings.tz).date())
        self.db.execute("UPDATE deliveries SET last_checked = ?, check_failures = 0, poll_note = '', "
                        "expected = CASE WHEN ? != '' THEN ? ELSE expected END WHERE id = ?",
                        (now, expected, expected, row["id"]))
        changed = self._set_status(self.db.one("SELECT * FROM deliveries WHERE id = ?", (row["id"],)), status,
                                   evidence, "page", now)
        diag.event("deliveries", f"checked {urlsplit(final).hostname}: {LABELS[status]}{' (changed)' if changed else ''}",
                   evidence=evidence)
        return {"ok": True, "status": status, "changed": changed}

    def _check_failed(self, row, why: str) -> dict:
        failures = row["check_failures"] + 1
        stop = failures >= 3
        note = ("I can't read this tracking page (it needs a full browser), so I'm following your emails for it "
                "instead." if stop else "")
        self.db.execute("UPDATE deliveries SET last_checked = ?, check_failures = ?, poll = ?, poll_note = ? WHERE id = ?",
                        (time.time(), failures, 0 if stop else row["poll"], note or row["poll_note"], row["id"]))
        diag.warning("deliveries", f"check {failures}/3 failed: {why}", url=row["tracking_url"])
        return {"ok": False, "detail": why, "stopped": stop}

    async def run(self) -> dict:
        """Hourly job: follow tracking links of active deliveries, tidy up old ones, send updates."""
        now = time.time()
        # delivered for 3 days, or nothing heard for 3 weeks → off the list
        self.db.execute("UPDATE deliveries SET active = 0 WHERE active = 1 AND ((status = 'delivered' AND updated < ?) "
                        "OR updated < ?)", (now - 3 * 86400, now - 21 * 86400))
        rows = self.db.all("SELECT id FROM deliveries WHERE active = 1 AND poll = 1 AND status != 'delivered' "
                           "AND tracking_url != '' AND (last_checked IS NULL OR last_checked < ?) ORDER BY last_checked "
                           "LIMIT 25", (now - 55 * 60,))
        checked = changed = 0
        for row in rows:
            result = await self.check(row["id"])
            checked += 1
            changed += int(bool(result.get("changed")))
        sent = await self.flush_notifications()
        active = self.db.one("SELECT COUNT(*) n FROM deliveries WHERE active = 1")["n"]
        return {"active": active, "checked": checked, "changed": changed, "notified": sent}

    # ------------------------------------------------------------------ for chat answers and the brief
    def summary_lines(self, only_today: bool = False) -> list[str]:
        lines = []
        today = datetime.now(self.settings.tz).date().isoformat()
        for item in self.active():
            if only_today and not (item["status"] == "out_for_delivery" or item["expected"] == today
                                   or item["status"] in {"attempted", "delayed"}):
                continue
            who = item["name"] + (f" ({item['retailer']})" if item["retailer"] and item["retailer"] != item["name"] else "")
            bits = [item["label"]]
            if item["expected_text"] and item["status"] != "delivered":
                bits.append(f"expected {item['expected_text']}")
            if item["carrier"]:
                bits.append(item["carrier"])
            link = f" — [track]({item['tracking_url']})" if item["tracking_url"] else ""
            lines.append(f"{item['icon']} **{one_line(who, 70)}**: {', '.join(bits)}{link}")
        return lines
