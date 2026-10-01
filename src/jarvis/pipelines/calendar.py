"""Tier 0 Calendar pipeline: store events, link attendees, journal entries and reminders."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..db import Database
from ..google.calendar import Calendar
from ..notify import Notifier
from ..vault.markdown import link, one_line, safe_name
from ..vault.writer import VaultWriter

FUTURE_DAYS = 60


def parse_when(value: str, tz) -> datetime:
    if len(value) == 10:  # all-day date
        return datetime.fromisoformat(value).replace(tzinfo=tz)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


class CalendarPipeline:
    def __init__(self, settings: Settings, db: Database, calendar: Calendar, writer: VaultWriter,
                 notifier: Notifier) -> None:
        self.settings = settings
        self.db = db
        self.calendar = calendar
        self.writer = writer
        self.notifier = notifier

    async def run(self) -> dict:
        now = datetime.now(timezone.utc)
        first = self.db.get("calendar.last_run") is None
        past = timedelta(days=self.settings.backfill_days if first else 2)
        changed = 0
        seen = 0
        for calendar_id in self.settings.google_calendar_ids:
            for event in await self.calendar.events(calendar_id, now - past, now + timedelta(days=FUTURE_DAYS)):
                seen += 1
                if self.upsert(event):
                    changed += 1
        self.db.set("calendar.last_run", now.timestamp())
        return {"seen": seen, "changed": changed, "backfill": first}

    def upsert(self, event: dict) -> bool:
        existing = self.db.one("SELECT * FROM events WHERE event_id = ?", (event["event_id"],))
        if existing is not None and existing["updated"] == event["updated"]:
            return False
        if existing is None and event["status"] == "cancelled":
            return False
        tz = self.settings.tz
        start = parse_when(event["start"], tz) if event["start"] else datetime.now(tz)
        local_start = start.astimezone(tz)
        if existing is None:
            path = (f"Sources/Calendar/{local_start:%Y}/{local_start:%Y-%m-%d} "
                    f"{safe_name(event['summary'] or 'Event', 70)} ({event['event_id'][-6:]}).md")
        else:
            path = existing["path"]
        reminded = 0 if existing is None or existing["start"] != event["start"] else existing["reminded"]
        self.db.execute(
            "INSERT OR REPLACE INTO events (event_id, calendar_id, path, summary, start, end, all_day, location, "
            "description, attendees, status, updated, html_link, reminded, ical_uid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event["event_id"], event["calendar_id"], path, event["summary"], event["start"], event["end"],
             int(event["all_day"]), event["location"], event["description"], json.dumps(event["attendees"]),
             event["status"], event["updated"], event["html_link"], reminded, event.get("ical_uid", "")),
        )
        for attendee in event["attendees"]:
            email = attendee.get("email", "")
            if attendee.get("self") or not email or email.endswith("calendar.google.com"):
                continue
            self.writer.ensure_person(email, attendee.get("name", ""), create=True)
        day = f"{local_start:%Y-%m-%d}"
        when = "all day" if event["all_day"] else f"{local_start:%H:%M}"
        status = " (cancelled)" if event["status"] == "cancelled" else ""
        text = f"Event ({when}): {link(path, one_line(event['summary'], 80) or 'event')}{status}"
        self.db.execute("DELETE FROM journal WHERE kind = 'event' AND ref = ?", (event["event_id"],))
        self.db.execute("INSERT OR REPLACE INTO journal (day, kind, ref, ts, text) VALUES (?, 'event', ?, ?, ?)",
                        (day, event["event_id"], local_start.timestamp(), text))
        if existing is not None and existing["start"][:10] != event["start"][:10]:
            old_day = parse_when(existing["start"], tz).astimezone(tz).strftime("%Y-%m-%d")
            self.db.queue_note("journal", old_day)
        self.db.queue_note("journal", day)
        self.db.queue_note("event", event["event_id"])
        return True

    async def remind(self) -> int:
        tz = self.settings.tz
        now = datetime.now(tz)
        horizon = now + timedelta(minutes=self.settings.notify_event_lead_minutes)
        sent = 0
        # `start` is ISO text, so a lexical lower bound (a day of slack for offsets) uses the events(start) index
        rows = self.db.all("SELECT * FROM events WHERE reminded = 0 AND all_day = 0 AND status != 'cancelled' "
                           "AND start >= ?", ((now - timedelta(days=1)).date().isoformat(),))
        for row in rows:
            start = parse_when(row["start"], tz)
            if not (now <= start <= horizon):
                continue
            minutes = max(0, int((start - now).total_seconds() // 60))
            details = [f"{start.astimezone(tz):%H:%M}"]
            if row["location"]:
                details.append(row["location"])
            await self.notifier.notify(
                title=f"In {minutes} min: {row['summary'] or 'Event'}",
                message=" · ".join(details),
                priority=4,
                url=row["html_link"],
                dedupe=f"event:{row['event_id']}:{row['start']}", category="calendar",
                tags="calendar",
            )
            self.db.execute("UPDATE events SET reminded = 1 WHERE event_id = ?", (row["event_id"],))
            sent += 1
        return sent
