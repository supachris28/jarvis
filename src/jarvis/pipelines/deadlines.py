"""Renewals, deadlines and replies you're waiting for — found in email by script (Tier 0, no model).

Deadlines: renewal notices (insurance, subscriptions, memberships, licences…), MOT reminders, "return by" dates,
trial endings and payment due dates. Each is remembered once (by kind, sender and date) and you're reminded ahead
of it — two weeks for renewals, three weeks for an MOT, a few days for returns and payments. They also appear in
the morning brief (next fortnight) and the evening preview (due tomorrow).

Waiting on a reply: emails you sent that ask something ("?", "could you…", "let me know…") where nobody has
written back in that conversation after FOLLOWUP_DAYS days. Listed in the brief; optional notification.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, time as dtime, timedelta

from .. import diag
from ..config import Settings
from ..db import Database
from ..extract.event_text import _find_date
from ..google.gmail import NOREPLY, app_email_url
from ..notify import Notifier
from ..vault.markdown import one_line

DATE_KINDS = ("iso", "num", "dmy", "mdy")
# (kind, label, icon, days of notice, pattern) — checked in this order; the first that has a date nearby wins
KINDS = [
    ("return", "Return by", "↩️", 3, re.compile(
        r"\b(?:return(?:s)?\s+(?:it\s+|items?\s+)?(?:by|before|until|no later than)|return window (?:closes|ends)|"
        r"eligible for (?:a )?(?:return|refund) (?:until|by|through)|return deadline|last day (?:to|for) returns?)\b",
        re.I)),
    ("trial", "Trial ends", "⏳", 2, re.compile(
        r"\b(?:free\s+)?trial\s+(?:period\s+)?(?:ends|will end|expires|is ending|runs out)\b", re.I)),
    ("mot", "MOT due", "🚗", 21, re.compile(
        r"\bMOT\b.{0,40}?\b(?:due|expires?|expiry|runs out|reminder)\b|\b(?:due|expires?)\b.{0,20}?\bMOT\b", re.I)),
    ("renewal", "Renewal", "🔁", 14, re.compile(
        r"\b(?:your|the)\s+(?:[\w&'-]+\s+){0,3}?(?:policy|insurance|cover|subscription|membership|plan|licen[cs]e|"
        r"warranty|domain|contract|tv licen[cs]e|road tax|vehicle tax)\b.{0,80}?\b(?:renews?|renewal|expires?|expiry|"
        r"ends|end date|will (?:auto-?)?renew)\b|\b(?:renewal date|expiry date|renews on|auto-?renews? on|"
        r"due for renewal|policy (?:ends|expires) on)\b", re.I)),
    ("payment", "Payment due", "💷", 3, re.compile(
        r"\b(?:payment|bill|amount|balance|invoice)\b.{0,40}?\b(?:is\s+|will be\s+)?(?:due|taken|collected|debited)"
        r"\s+(?:on|by)\b|\bpay(?:ment)? (?:by|before)\b|\bdue date\b|\bdirect debit\b.{0,60}?\b(?:on|taken)\b", re.I)),
]
LOOK_BACK_TERMS = ['renew', 'renewal', 'expires', 'expiry', '"return by"', '"return window"', '"trial ends"',
                   '"payment due"', '"due date"', 'MOT']
SKIP_LABELS = {"SPAM", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
MARKETING = re.compile(r"\b(sale|% off|discount|offer ends|limited time|deal|voucher code)\b", re.I)

# a question or request in an email you sent
REQUEST = re.compile(
    r"\?|\b(could you|can you|would you|will you|please (?:let me know|confirm|send|advise|reply|get back|check)|"
    r"let me know|get back to me|are you (?:able|free|around|ok)|do you (?:know|have|want|fancy)|"
    r"when (?:can|could|would|will) you|any (?:news|update)|look(?:ing)? forward to hearing)\b", re.I)
QUOTE_START = re.compile(r"^(?:On .{0,300}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|From:\s.+$|_{5,})",
                         re.I | re.M)


def own_text(body: str) -> str:
    """What you wrote, without the quoted conversation below it."""
    match = QUOTE_START.search(body or "")
    text = body[:match.start()] if match else body or ""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def find_deadline(subject: str, body: str, today: date) -> tuple[str, date, str] | None:
    """(kind, due date, the sentence it came from) for the first deadline with a date close by."""
    text = re.sub(r"[ \t ]+", " ", f"{subject}\n{body}")
    for kind, _label, _icon, _lead, pattern in KINDS:
        for match in pattern.finditer(text):
            window = text[match.start(): match.end() + 160]
            due, _ = _find_date(window, today, DATE_KINDS)
            if due is None or not today <= due <= today + timedelta(days=400):
                continue
            start = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 80)
            evidence = one_line(text[start: match.end() + 120], 200)
            return kind, due, evidence
    return None


class Deadlines:
    def __init__(self, settings: Settings, db: Database, notifier: Notifier, gmail=None) -> None:
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.gmail = gmail
        with db._lock:
            db._conn.executescript("""
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created REAL NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    org TEXT NOT NULL DEFAULT '',
                    due TEXT NOT NULL,                 -- local date
                    remind_at REAL NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'upcoming'   -- upcoming | reminded | done | dismissed | past
                );
                CREATE UNIQUE INDEX IF NOT EXISTS deadlines_key ON deadlines(kind, org, due);
                CREATE TABLE IF NOT EXISTS followups (
                    thread_id TEXT PRIMARY KEY,
                    sent_ts REAL NOT NULL,
                    status TEXT NOT NULL              -- notified | dismissed
                );
            """)

    # ---------------------------------------------------------------- deadlines
    def on_message(self, message, announce: bool = True) -> int | None:
        if (message.outgoing and not message.forwarded_from) or SKIP_LABELS.intersection(message.labels):
            return None
        if MARKETING.search(message.subject):
            return None
        tz = self.settings.tz
        found = find_deadline(message.subject, message.body, datetime.now(tz).date())
        if found is None:
            return None
        kind, due, evidence = found
        lead = next(k[3] for k in KINDS if k[0] == kind)
        remind = datetime.combine(due - timedelta(days=lead), dtime(9, 0), tz).timestamp()
        org = one_line(message.sender_name or message.from_addr, 60)
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO deadlines (created, kind, title, org, due, remind_at, message_id, thread_id, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, one_line(message.subject, 90) or "(no subject)", org, due.isoformat(), remind,
             message.message_id, message.thread_id, evidence))
        if not cursor.rowcount:
            return None
        diag.event("deadlines", f"{kind} on {due}: {message.subject[:70]}", sender=org, evidence=evidence)
        return cursor.lastrowid

    async def look_back(self, days: int = 365, limit: int = 300) -> dict:
        """Read renewal-ish emails from the past year (bulk ones aren't stored in full, so Gmail is asked)."""
        from ..google.gmail import parse_message
        if self.gmail is None:
            return {"error": "Gmail isn't connected."}
        query = f"newer_than:{int(days)}d -category:promotions -category:social ({' OR '.join(LOOK_BACK_TERMS)})"
        ids = await self.gmail.list_message_ids(query, limit)
        found = 0
        for message_id in ids:
            try:
                message = parse_message(await self.gmail.message(message_id), self.settings.email_body_limit)
            except Exception as error:  # noqa: BLE001 — one unreadable email doesn't stop the look-back
                diag.debug("deadlines", f"skipped an email in the look-back: {type(error).__name__}", id=message_id)
                continue
            if self.on_message(message, announce=False) is not None:
                found += 1
        self.db.set("deadlines.looked_back", time.time())
        diag.event("deadlines", f"looked back {days} days: {len(ids)} candidate email(s), {found} new deadline(s)")
        return {"emails": len(ids), "found": found}

    def upcoming(self, days: int | None = None) -> list[dict]:
        today = datetime.now(self.settings.tz).date()
        rows = self.db.all("SELECT * FROM deadlines WHERE status IN ('upcoming', 'reminded') AND due >= ? "
                           "ORDER BY due, id", (today.isoformat(),))
        items = []
        for row in rows:
            due = date.fromisoformat(row["due"])
            if days is not None and (due - today).days > days:
                continue
            kind = next(k for k in KINDS if k[0] == row["kind"])
            away = (due - today).days
            items.append(dict(row) | {
                "label": kind[1], "icon": kind[2], "days": away,
                "when": f"{due:%a} {due.day} {due:%b}" + (f" {due:%Y}" if due.year != today.year else ""),
                "away": "today" if away == 0 else "tomorrow" if away == 1 else f"in {away} days",
                "email_url": app_email_url(row["thread_id"]) if row["thread_id"] else ""})
        return items

    def lines(self, days: int = 14) -> list[str]:
        return [f"{d['icon']} **{d['label']}** {d['when']} ({d['away']}) — {d['title']} · {d['org']}"
                for d in self.upcoming(days)]

    def set_status(self, deadline_id: int, status: str) -> bool:
        if status not in ("upcoming", "done", "dismissed"):
            return False
        return bool(self.db.execute("UPDATE deadlines SET status = ? WHERE id = ?", (status, deadline_id)).rowcount)

    # ---------------------------------------------------------------- waiting on a reply
    def waiting(self) -> list[dict]:
        """Threads where your latest email asked something and nobody has replied for FOLLOWUP_DAYS days."""
        now = time.time()
        wait = max(1, self.settings.followup_days) * 86400
        me = (self.db.get("gmail.me") or "").casefold()
        rows = self.db.all(
            "SELECT e.* FROM emails e WHERE e.outgoing = 1 AND e.ts < ? AND e.ts > ? "
            "AND e.ts = (SELECT MAX(ts) FROM emails x WHERE x.thread_id = e.thread_id) "
            "AND NOT EXISTS (SELECT 1 FROM followups f WHERE f.thread_id = e.thread_id AND f.status = 'dismissed' "
            "                AND f.sent_ts >= e.ts) "
            "ORDER BY e.ts", (now - wait, now - 21 * 86400))
        items = []
        for row in rows:
            try:
                to = [(str(a).casefold(), str(n)) for a, n in json.loads(row["to_addrs"] or "[]")]
            except (ValueError, TypeError):
                to = []
            people = [(a, n) for a, n in to if a and a != me and not NOREPLY.search(a)
                      and not a.endswith("calendar.google.com")]
            if not people or not REQUEST.search(own_text(row["body"])[:3000]):
                continue
            name = people[0][1] or people[0][0].split("@")[0]
            if len(people) > 1:
                name += f" +{len(people) - 1}"
            sent = datetime.fromtimestamp(row["ts"], self.settings.tz)
            days = int((now - row["ts"]) // 86400)
            items.append({"thread_id": row["thread_id"], "to": name, "subject": one_line(row["subject"], 80) or
                          "(no subject)", "sent_ts": row["ts"], "sent": f"{sent:%a} {sent.day} {sent:%b}",
                          "days": days, "email_url": app_email_url(row["thread_id"])})
        return items

    def waiting_lines(self) -> list[str]:
        return [f"{w['to']} — [{w['subject']}]({w['email_url']}) (you asked {w['sent']}, {w['days']} days ago)"
                for w in self.waiting()]

    def dismiss_waiting(self, thread_id: str) -> None:
        row = self.db.one("SELECT MAX(ts) AS ts FROM emails WHERE thread_id = ? AND outgoing = 1", (thread_id,))
        self.db.execute("INSERT OR REPLACE INTO followups (thread_id, sent_ts, status) VALUES (?, ?, 'dismissed')",
                        (thread_id, (row["ts"] if row and row["ts"] else time.time())))

    # ---------------------------------------------------------------- the hourly job
    async def run(self) -> dict:
        looked = None
        if not self.db.get("deadlines.looked_back") and self.gmail is not None:
            looked = await self.look_back()   # first run: the past year — one summary instead of a flood
            soon = self.db.all("SELECT id FROM deadlines WHERE status = 'upcoming' AND remind_at < ?", (time.time(),))
            if soon:
                self.db.execute("UPDATE deadlines SET status = 'reminded' WHERE remind_at < ?", (time.time(),))
                await self.notifier.notify(f"{len(soon)} renewal(s) or deadline(s) coming up",
                                           "Found in your email — see Jarvis → Plan.", 3,
                                           self.settings.public_url.rstrip("/") + "/#plan",
                                           dedupe="deadlines:first-run", category="deadlines")
        today = datetime.now(self.settings.tz).date()
        self.db.execute("UPDATE deadlines SET status = 'past' WHERE status IN ('upcoming', 'reminded') AND due < ?",
                        (today.isoformat(),))
        reminded = 0
        for row in self.db.all("SELECT * FROM deadlines WHERE status = 'upcoming' AND remind_at <= ? ORDER BY due",
                               (time.time(),)):
            item = next((d for d in self.upcoming() if d["id"] == row["id"]), None)
            if item is None:
                continue
            public = self.settings.public_url.rstrip("/")
            await self.notifier.notify(
                f"{item['label']} {item['when']} ({item['away']})", f"{item['title']} — {item['org']}", 3,
                app_email_url(row["thread_id"], public) if row["thread_id"] else public + "/#plan",
                dedupe=f"deadline:{row['id']}", tags="date", category="deadlines")
            self.db.execute("UPDATE deadlines SET status = 'reminded' WHERE id = ?", (row["id"],))
            reminded += 1
        nudged = 0
        for item in self.waiting():
            known = self.db.one("SELECT 1 FROM followups WHERE thread_id = ? AND sent_ts >= ?",
                                (item["thread_id"], item["sent_ts"]))
            if known:
                continue
            self.db.execute("INSERT OR REPLACE INTO followups (thread_id, sent_ts, status) VALUES (?, ?, 'notified')",
                            (item["thread_id"], item["sent_ts"]))
            await self.notifier.notify(f"No reply yet from {item['to']}",
                                       f"{item['subject']} — you asked {item['sent']}", 2,
                                       app_email_url(item["thread_id"], self.settings.public_url.rstrip("/")),
                                       dedupe=f"followup:{item['thread_id']}:{int(item['sent_ts'])}",
                                       category="followups")
            nudged += 1
        result = {"reminded": reminded, "waiting_nudged": nudged, "upcoming": len(self.upcoming())}
        if looked:
            result["looked_back"] = looked
        return result
