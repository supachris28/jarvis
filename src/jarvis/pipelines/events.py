"""Find events in email and propose adding them to Google Calendar.

Order of preference (cheapest first):
1. iCalendar invitations / .ics attachments           — script, confidence 1.0
2. schema.org JSON-LD reservations in HTML email        — script, confidence 0.95
3. Local LLM extraction, only for emails that pass a    — queued, batched, daily budget,
   date+event keyword prefilter                           no tools, schema-validated output

Nothing is added to the calendar without confirmation (Events tab or chat), unless
EVENTS_AUTO_ADD=ics is set, which auto-adds exact .ics invitations only.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta

from .. import diag
from ..config import Settings
from ..db import Database
from ..extract.events import (EXTRACT_PROMPT, EventCandidate, parse_ics, parse_iso, parse_jsonld, similar_titles,
                              is_not_event, title_tokens, validate_llm_events, worth_llm_scan)
from ..extract.event_text import _find_date, calendar_name, parse_event_request
from ..google.calendar import Calendar
from ..google.gmail import Gmail, ParsedMessage, app_email_url, thread_url
from ..google.oauth import GoogleError
from ..llm import LLMError, Ollama
from ..notify import Notifier
from ..vault.markdown import link, one_line

log = logging.getLogger(__name__)
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

CHAT_PROMPT = """
Extract the calendar event Chris is asking to add from his message. Return ONLY JSON:
{"events":[{"title":"...","start":"YYYY-MM-DDTHH:MM or YYYY-MM-DD","end":"... or empty",
"all_day":true|false,"location":"...","notes":"","confidence":0.0-1.0}]}
Resolve relative dates using the current date given. Dates are UK style: 31/10/2026 and 3/11 are day/month.
Everything that isn't the date or time is the title. If no date is given, return {"events":[]}.
""".strip()


ADDRESS = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")


def sender_address(sender: str) -> str:
    """'Vinted <no-reply@vinted.co.uk>' → 'no-reply@vinted.co.uk'."""
    match = ADDRESS.search(sender or "")
    return (match.group(1) if match else (sender or "")).strip().lower()


def next_day(day: str) -> str:
    """'2026-10-01' → '2026-10-02', for half-open ISO range queries on text columns."""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def fingerprint(title: str, start: str) -> str:
    key = " ".join(sorted(title_tokens(title))) + "|" + start[:16]
    return hashlib.sha1(key.encode()).hexdigest()


def describe_when(start: str, end: str, all_day: bool, tz) -> str:
    if all_day:
        first = date.fromisoformat(start[:10])
        last = date.fromisoformat(end[:10]) - timedelta(days=1) if end else first
        if last > first:
            return f"{first:%a %d %b} – {last:%a %d %b %Y} (all day)"
        return f"{first:%a %d %b %Y} (all day)"
    begin = parse_iso(start, tz).astimezone(tz)
    finish = parse_iso(end, tz).astimezone(tz) if end else None
    text = f"{begin:%a %d %b %Y, %H:%M}"
    if finish:
        text += f"–{finish:%H:%M}" if finish.date() == begin.date() else f" – {finish:%a %d %b %H:%M}"
    return text


class EventFinder:
    def __init__(self, settings: Settings, db: Database, gmail: Gmail, calendar: Calendar, llm: Ollama,
                 notifier: Notifier) -> None:
        self.settings = settings
        self.db = db
        self.gmail = gmail
        self.calendar = calendar
        self.llm = llm
        self.notifier = notifier
        self._pending_notifications: list[int] = []
        self.last_calendar_problem = ""

    # ---------------------------------------------------------------- ingestion hook
    async def on_message(self, message: ParsedMessage, notify: bool = True) -> int:
        """Called by the Gmail pipeline for every new message. Returns proposals created."""
        if message.promotional or "DRAFT" in message.labels:
            return 0
        candidates: list[EventCandidate] = []
        ics_texts = list(message.ics_inline)
        for attachment_id in message.ics_attachment_ids[:3]:
            try:
                ics_texts.append(await self.gmail.attachment(message.message_id, attachment_id))
            except GoogleError as error:
                log.info("could not fetch .ics attachment: %s", error)
        for text in ics_texts:
            candidates += parse_ics(text, self.settings.tz)
        if message.html:
            candidates += parse_jsonld(message.html, self.settings.tz)
        created = 0
        if candidates:
            diag.event("events", f"{len(candidates)} event(s) found by script in “{message.subject[:60]}”",
                       found=[f"{c.source}: {c.title} @ {c.start}" for c in candidates])
        for candidate in candidates:
            if self.propose(candidate, message, notify=notify):
                created += 1
        if candidates:
            return created
        automated = bool(message.bulk or "CATEGORY_UPDATES" in message.labels)
        sender = (message.from_addr or "").lower()
        if self.is_muted(sender):
            diag.debug("events", f"sender muted for events, not scanned: {message.subject[:60]}", sender=sender)
            return created
        worth, reason = worth_llm_scan(message.subject, message.body, automated)
        diag.debug("events", f"{'queued for model' if worth else 'not scanned'} ({reason}): {message.subject[:60]}",
                   id=message.message_id, automated=automated)
        if worth:
            self.db.execute(
                "INSERT OR IGNORE INTO event_scan (message_id, queued, status, body, subject, sender, ts, thread_id, "
                "automated) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (message.message_id, time.time(), message.body[:6000], message.subject,
                 f"{message.from_name} <{message.from_addr}>", message.ts, message.thread_id, int(automated)),
            )
        return created

    # ---------------------------------------------------------------- Tier 1 queue
    def budget_left(self) -> int:
        today = datetime.now(self.settings.tz).strftime("%Y-%m-%d")
        used = self.db.get(f"events.llm.{today}", 0)
        return max(0, self.settings.events_llm_daily_limit - int(used))

    def _spend(self) -> None:
        today = datetime.now(self.settings.tz).strftime("%Y-%m-%d")
        self.db.set(f"events.llm.{today}", int(self.db.get(f"events.llm.{today}", 0)) + 1)

    async def scan_queue(self, limit: int = 20) -> dict:
        pending = self.db.one("SELECT COUNT(*) n FROM event_scan WHERE status = 'pending'")["n"]
        if not pending:
            return {"scanned": 0, "proposed": 0, "pending": 0}
        if not (await self.llm.health()).get("ok"):
            diag.debug("events", f"{pending} email(s) waiting — model offline")
            return {"scanned": 0, "proposed": 0, "pending": pending, "waiting": "model offline"}
        if not self.budget_left():
            diag.warning("events", f"daily model budget used up ({self.settings.events_llm_daily_limit}); "
                         f"{pending} email(s) wait until tomorrow")
        rows = self.db.all("SELECT * FROM event_scan WHERE status = 'pending' ORDER BY ts DESC LIMIT ?",
                           (min(limit, self.budget_left()),))
        scanned = proposed = 0
        now = datetime.now(self.settings.tz)
        for row in rows:
            if row["ts"] < time.time() - 60 * 86400:
                self.db.execute("UPDATE event_scan SET status = 'skipped', body = '' WHERE message_id = ?",
                                (row["message_id"],))
                continue
            sent = datetime.fromtimestamp(row["ts"], self.settings.tz)
            user = (f"Email sent: {sent:%A %d %B %Y %H:%M}\nFrom: {row['sender']}\nSubject: {row['subject']}\n\n"
                    f"<<<\n{row['body']}\n>>>")
            try:
                raw = await self.llm.chat([{"role": "system", "content": EXTRACT_PROMPT},
                                           {"role": "user", "content": user}], json_mode=True)
            except LLMError:
                break
            self._spend()
            scanned += 1
            context = {"message_id": row["message_id"], "thread_id": row["thread_id"], "subject": row["subject"],
                       "from_addr": sender_address(row["sender"])}
            # automated senders (receipts, notices) need a surer answer than people writing to Chris
            valid = validate_llm_events(raw, self.settings.tz, now, min_confidence=0.75 if row["automated"] else 0.6)
            diag.event("events", f"model read “{row['subject'][:60]}”: {len(valid)} usable event(s)",
                       raw=raw[:1500], usable=[f"{c.title} @ {c.start} ({c.confidence})" for c in valid])
            for candidate in valid:
                if self.propose(candidate, context):
                    proposed += 1
            self.db.execute("UPDATE event_scan SET status = 'done', body = '' WHERE message_id = ?",
                            (row["message_id"],))
        left = self.db.one("SELECT COUNT(*) n FROM event_scan WHERE status = 'pending'")["n"]
        return {"scanned": scanned, "proposed": proposed, "pending": left, "budget_left": self.budget_left()}

    # ---------------------------------------------------------------- proposals
    def _is_future(self, candidate: EventCandidate) -> bool:
        tz = self.settings.tz
        if candidate.all_day:
            end = date.fromisoformat((candidate.end or candidate.start)[:10])
            return end > datetime.now(tz).date()
        return candidate.start_dt(tz) > datetime.now(tz) - timedelta(hours=1)

    def _already_on_calendar(self, candidate: EventCandidate) -> bool:
        if candidate.ical_uid and self.db.one("SELECT 1 FROM events WHERE ical_uid = ? AND status != 'cancelled'",
                                              (candidate.ical_uid,)):
            return True
        day = candidate.start[:10]
        for row in self.db.all("SELECT summary, start FROM events WHERE start >= ? AND start < ? AND status != 'cancelled'",
                               (day, next_day(day))):
            if similar_titles(row["summary"], candidate.title):
                return True
        return False

    def propose(self, candidate: EventCandidate, message, notify: bool = True) -> int | None:
        """Store a proposal. `message` is a ParsedMessage or a dict with message_id/thread_id/subject."""
        if not self._is_future(candidate):
            diag.debug("events", f"skipped past event: {candidate.title} @ {candidate.start}")
            return None
        get = (lambda k: message.get(k, "")) if isinstance(message, dict) else (lambda k: getattr(message, k, ""))
        sender = (get("from_addr") or "").lower()
        if candidate.source == "llm" and is_not_event(get("subject")):
            diag.debug("events", f"skipped — email looks like a notice, not an event: {get('subject')[:60]}")
            return None
        if candidate.source == "llm" and self.is_muted(sender):
            diag.debug("events", f"skipped — sender muted: {sender}")
            return None
        print_ = fingerprint(candidate.title, candidate.start)
        if self.db.one("SELECT 1 FROM event_proposals WHERE fingerprint = ?", (print_,)):
            diag.debug("events", f"already proposed: {candidate.title} @ {candidate.start}")
            return None
        for row in self.db.all("SELECT title FROM event_proposals WHERE start >= ? AND start < ?",
                               (candidate.start[:10], next_day(candidate.start[:10]))):
            if similar_titles(row["title"], candidate.title):
                diag.debug("events", f"similar proposal exists that day: {candidate.title} ≈ {row['title']}")
                return None
        status = "duplicate" if self._already_on_calendar(candidate) else "pending"
        diag.event("events", f"{'already in calendar' if status == 'duplicate' else 'proposed'}: {candidate.title} "
                   f"@ {candidate.start}", found_by=candidate.source, confidence=candidate.confidence,
                   email=get("subject"))
        cursor = self.db.execute(
            "INSERT INTO event_proposals (created, source, fingerprint, message_id, thread_id, email_subject, ical_uid, "
            "title, start, end, all_day, location, notes, confidence, status, sender) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), candidate.source, print_, get("message_id"), get("thread_id"), get("subject"),
             candidate.ical_uid, candidate.title, candidate.start, candidate.end, int(candidate.all_day),
             candidate.location, candidate.notes, candidate.confidence, status, sender),
        )
        proposal_id = cursor.lastrowid
        if status != "pending":
            return None
        if self.settings.events_auto_add == "ics" and candidate.source == "ics":
            return proposal_id  # accepted by the job that runs auto-adds (needs async)
        if notify:
            self._pending_notifications.append(proposal_id)
        return proposal_id

    async def flush_notifications(self) -> int:
        ids, self._pending_notifications = self._pending_notifications, []
        for proposal_id in ids:
            row = self.get(proposal_id)
            if not row or row["status"] != "pending":
                continue
            where = f" · {row['location']}" if row["location"] else ""
            await self.notifier.notify(
                title=f"Add to calendar? {one_line(row['title'], 60)}",
                message=f"{describe_when(row['start'], row['end'], row['all_day'], self.settings.tz)}{where}\n"
                        f"From email: {one_line(row['email_subject'], 80) or '(chat)'}",
                priority=3, url=self.settings.public_url.rstrip("/") + "/#events",
                dedupe=f"proposal:{proposal_id}", tags="calendar", category="events",
            )
        return len(ids)

    async def auto_add(self) -> int:
        if self.settings.events_auto_add != "ics":
            return 0
        added = 0
        for row in self.db.all("SELECT id FROM event_proposals WHERE status = 'pending' AND source = 'ics'"):
            try:
                await self.accept(row["id"], actor="auto")
                added += 1
            except GoogleError as error:
                log.warning("auto-add failed: %s", error)
        return added

    def get(self, proposal_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM event_proposals WHERE id = ?", (proposal_id,))
        return self.present(row) if row else None

    def present(self, row) -> dict:
        item = dict(row)
        item["all_day"] = bool(item["all_day"])
        item["when"] = describe_when(item["start"], item["end"], item["all_day"], self.settings.tz)
        item["gmail_url"] = thread_url(item["thread_id"], self.db.get("gmail.me", ""))
        item["email_url"] = app_email_url(item["thread_id"])
        thread = self.db.one("SELECT path FROM threads WHERE thread_id = ?", (item["thread_id"],)) \
            if item["thread_id"] else None
        item["note_path"] = thread["path"] if thread else ""
        return item

    def list(self, status: str = "pending", limit: int = 100) -> list[dict]:
        if status == "all":
            rows = self.db.all("SELECT * FROM event_proposals WHERE status != 'duplicate' ORDER BY id DESC LIMIT ?",
                               (limit,))
        else:
            rows = self.db.all("SELECT * FROM event_proposals WHERE status = ? ORDER BY start LIMIT ?", (status, limit))
        return [self.present(r) for r in rows]

    async def accept(self, proposal_id: int, overrides: dict | None = None, actor: str = "user") -> dict:
        row = self.db.one("SELECT * FROM event_proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise GoogleError("Unknown proposal.")
        if row["status"] == "added":
            return self.get(proposal_id)
        if not self.calendar.oauth.has_scope(WRITE_SCOPE):
            raise GoogleError("Jarvis doesn't have permission to add events yet. Open Status → Connect Google "
                              "and approve calendar access again.")
        if row["kind"] == "note":
            return await self._add_note(row, (overrides or {}).get("notes") or row["notes"])
        data = dict(row)
        for key in ("title", "start", "end", "location", "notes", "all_day"):
            if overrides and key in overrides and overrides[key] is not None:
                data[key] = overrides[key]
        data["all_day"] = bool(data["all_day"])
        tz = self.settings.tz
        try:
            if data["all_day"]:
                start_d = date.fromisoformat(str(data["start"])[:10])
                end_d = date.fromisoformat(str(data["end"])[:10]) if data["end"] else start_d + timedelta(days=1)
                if end_d <= start_d:
                    end_d = start_d + timedelta(days=1)
                data["start"], data["end"] = start_d.isoformat(), end_d.isoformat()
                timing = {"start": {"date": data["start"]}, "end": {"date": data["end"]}}
            else:
                start_dt = parse_iso(str(data["start"]), tz)
                end_dt = parse_iso(str(data["end"]), tz) if data["end"] else start_dt + timedelta(hours=1)
                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(hours=1)
                data["start"], data["end"] = start_dt.isoformat(), end_dt.isoformat()
                timing = {"start": {"dateTime": data["start"], "timeZone": self.settings.timezone},
                          "end": {"dateTime": data["end"], "timeZone": self.settings.timezone}}
        except ValueError as error:
            raise GoogleError(f"Invalid date/time: {error}") from None
        description = (data.get("notes") or "").strip()
        body = {"summary": str(data["title"])[:250], "location": str(data.get("location") or "")[:500], **timing}
        if row["thread_id"]:
            gmail_url = thread_url(row["thread_id"], self.db.get("gmail.me", ""))
            description += (f"\n\nAdded by Jarvis from the email “{row['email_subject']}”: {gmail_url}"
                            f"\nOpen it in Jarvis: {app_email_url(row['thread_id'], self.settings.public_url)}")
            body["source"] = {"title": "Email", "url": gmail_url}
        else:
            description += "\n\nAdded by Jarvis."
        body["description"] = description.strip()
        diag.event("events", f"adding to Google Calendar: {body['summary']}", body=body, actor=actor)
        try:
            created = await self.calendar.insert(row["calendar_id"] or self.settings.events_calendar_id, body)
        except GoogleError as error:
            diag.error("events", f"calendar insert failed: {error}")
            self.db.execute("UPDATE event_proposals SET status = 'failed', error = ? WHERE id = ?",
                            (str(error)[:300], proposal_id))
            raise
        self.db.execute(
            "UPDATE event_proposals SET status = 'added', event_id = ?, decided = ?, title = ?, start = ?, end = ?, "
            "all_day = ?, location = ?, error = '' WHERE id = ?",
            (created.get("id", ""), time.time(), data["title"], data["start"], data["end"], int(data["all_day"]),
             data.get("location") or "", proposal_id),
        )
        self._sender_decision(row["sender"], accepted=True)
        now = datetime.now(tz)
        source = ""
        thread = self.db.one("SELECT path FROM threads WHERE thread_id = ?", (row["thread_id"],)) if row["thread_id"] else None
        if thread:
            source = f" (from {link(thread['path'], one_line(row['email_subject'], 60) or 'email')})"
        text = (f"Added to calendar{' automatically' if actor == 'auto' else ''}: **{data['title']}** — "
                f"{describe_when(data['start'], data['end'], data['all_day'], tz)}{source}")
        self.db.execute("INSERT OR REPLACE INTO journal (day, kind, ref, ts, text) VALUES (?, 'calendar-add', ?, ?, ?)",
                        (f"{now:%Y-%m-%d}", str(proposal_id), now.timestamp(), text))
        self.db.queue_note("journal", f"{now:%Y-%m-%d}")
        result = self.get(proposal_id)
        result["html_link"] = created.get("htmlLink", "")
        return result

    async def _add_note(self, row, text: str) -> dict:
        """Append a line (e.g. a booking reference) to an existing event's description."""
        try:
            event = await self.calendar.get_event(row["calendar_id"], row["target_event_id"])
            description = (event.get("description") or "").rstrip()
            if text.strip() not in description:
                description = f"{description}\n\n{text.strip()}".strip()
                await self.calendar.patch(row["calendar_id"], row["target_event_id"], {"description": description})
        except GoogleError as error:
            diag.error("events", f"couldn't update “{row['title']}”: {error}")
            self.db.execute("UPDATE event_proposals SET status = 'failed', error = ? WHERE id = ?",
                            (str(error)[:300], row["id"]))
            raise
        self.db.execute("UPDATE events SET description = ? WHERE event_id = ?", (description, row["target_event_id"]))
        self.db.execute("UPDATE event_proposals SET status = 'added', decided = ?, notes = ?, error = '' WHERE id = ?",
                        (time.time(), text, row["id"]))
        diag.event("events", f"added to “{row['title']}”: {text}")
        result = self.get(row["id"])
        result["html_link"] = event.get("htmlLink", "")
        return result

    def dismiss(self, proposal_id: int, mute: bool = False) -> dict:
        """Dismiss a proposal. Senders whose suggestions keep getting dismissed stop being scanned."""
        row = self.db.one("SELECT sender, status FROM event_proposals WHERE id = ?", (proposal_id,))
        self.db.execute("UPDATE event_proposals SET status = 'dismissed', decided = ? WHERE id = ? AND status != 'added'",
                        (time.time(), proposal_id))
        muted = False
        if row and row["status"] == "pending":
            muted = self._sender_decision(row["sender"], accepted=False, mute=mute)
        return {"muted": muted, "sender": row["sender"] if row else ""}

    # ---------------------------------------------------------------- sender learning
    def is_muted(self, sender: str) -> bool:
        if not sender:
            return False
        row = self.db.one("SELECT muted FROM event_senders WHERE sender = ?", (sender.lower(),))
        return bool(row and row["muted"])

    def _sender_decision(self, sender: str, accepted: bool, mute: bool = False) -> bool:
        sender = (sender or "").lower()
        if not sender:
            return False
        column = "accepted" if accepted else "dismissed"
        self.db.execute(f"INSERT INTO event_senders (sender, {column}, updated) VALUES (?, 1, ?) "
                        f"ON CONFLICT(sender) DO UPDATE SET {column} = {column} + 1, updated = excluded.updated",
                        (sender, time.time()))
        if accepted:
            return False
        stats = self.db.one("SELECT dismissed, accepted, muted FROM event_senders WHERE sender = ?", (sender,))
        if stats["muted"]:
            return True
        if mute or (stats["dismissed"] >= 2 and stats["accepted"] == 0):
            self.mute(sender, reason="you asked" if mute else f"{stats['dismissed']} suggestions dismissed")
            return True
        return False

    def mute(self, sender: str, reason: str = "") -> int:
        sender = sender.lower()
        self.db.execute("INSERT INTO event_senders (sender, muted, updated) VALUES (?, 1, ?) "
                        "ON CONFLICT(sender) DO UPDATE SET muted = 1, updated = excluded.updated", (sender, time.time()))
        cleared = self.db.execute("UPDATE event_proposals SET status = 'dismissed', decided = ?, "
                                  "error = 'auto: sender muted' "
                                  "WHERE sender = ? AND status = 'pending' AND source = 'llm'",
                                  (time.time(), sender)).rowcount
        diag.event("events", f"stopped suggesting events from {sender}", reason=reason, cleared=cleared)
        return cleared

    def unmute(self, sender: str) -> None:
        self.db.execute("UPDATE event_senders SET muted = 0, dismissed = 0, updated = ? WHERE sender = ?",
                        (time.time(), sender.lower()))
        diag.event("events", f"events from {sender} will be suggested again")

    def muted_senders(self) -> list[dict]:
        return [dict(r) for r in self.db.all(
            "SELECT sender, dismissed, accepted, updated FROM event_senders WHERE muted = 1 ORDER BY updated DESC")]

    def cleanup_noise(self) -> int:
        """Dismiss pending model suggestions from emails that are clearly notices (T&Cs, policies, offers)."""
        for row in self.db.all("SELECT p.id, s.sender FROM event_proposals p JOIN event_scan s "
                               "ON s.message_id = p.message_id WHERE p.sender = '' AND p.source = 'llm'"):
            self.db.execute("UPDATE event_proposals SET sender = ? WHERE id = ?", (sender_address(row["sender"]), row["id"]))
        cleared = 0
        for row in self.db.all("SELECT id, email_subject FROM event_proposals WHERE status = 'pending' AND source = 'llm'"):
            if is_not_event(row["email_subject"]):
                self.db.execute("UPDATE event_proposals SET status = 'dismissed', decided = ?, "
                                "error = 'auto: notice email' WHERE id = ?", (time.time(), row["id"]))
                cleared += 1
        if cleared:
            diag.event("events", f"cleared {cleared} suggestion(s) from notice emails (terms, policies, offers)")
        return cleared

    def find_event(self, target: str) -> tuple[dict | None, str]:
        """'bowling on Saturday' → the matching event from your synced calendars, or (None, why not)."""
        now = datetime.now(self.settings.tz)
        day, rest = _find_date(target, now.date())
        words = title_tokens(rest) - {"the", "and", "event", "this", "next", "with", "for"}
        if not words:
            return None, ""
        rows = self.db.all("SELECT * FROM events WHERE status != 'cancelled' AND start >= ? "
                           "ORDER BY start LIMIT 400", (now.date().isoformat(),))
        if day is not None:
            rows = [r for r in rows if r["start"][:10] == day.isoformat()]
        scored = sorted(((len(words & title_tokens(r["summary"])), r) for r in rows), key=lambda x: -x[0])
        if not scored or scored[0][0] == 0:
            when = f" on {day:%a %d %b}" if day else " coming up"
            return None, f"I couldn't find a “{' '.join(sorted(words))}” event{when} in your calendar."
        return dict(scored[0][1]), ""

    def propose_note(self, event: dict, text: str) -> dict:
        """A card asking to add `text` to an existing event's description."""
        cursor = self.db.execute(
            "INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, location, notes, "
            "confidence, status, calendar_id, kind, target_event_id) "
            "VALUES (?, 'chat', ?, ?, ?, ?, ?, ?, ?, 1.0, 'pending', ?, 'note', ?)",
            (time.time(), f"note:{event['event_id']}:{time.time()}", event["summary"], event["start"], event["end"],
             int(event["all_day"]), event.get("location", ""), text, event["calendar_id"], event["event_id"]))
        diag.event("events", f"proposed adding to “{event['summary']}”: {text}", event_id=event["event_id"])
        return self.get(cursor.lastrowid)

    async def find_calendar(self, name: str) -> tuple[str, str, str]:
        """'Family' → (calendar id, its name, problem or ''). Falls back to the default calendar."""
        if not name:
            return "", "", ""
        try:
            # hidden calendars only when you've ticked them on the Status page (e.g. a shared Family calendar)
            chosen = set(self.settings.google_calendar_ids)
            calendars = [c for c in await self.calendar.calendars()
                         if c["writable"] and (not c["hidden"] or c["id"] in chosen)]
        except GoogleError as error:
            return "", "", f"I couldn't list your calendars ({error}), so this goes in your main calendar."
        wanted = name.casefold()
        for test in (lambda c: c["name"].casefold() == wanted,
                     lambda c: c["name"].casefold().startswith(wanted) or wanted.startswith(c["name"].casefold()),
                     lambda c: wanted in c["name"].casefold() or c["name"].casefold() in wanted):
            found = [c for c in calendars if c["name"] and test(c)]
            if len(found) == 1:
                return found[0]["id"], found[0]["name"], ""
        names = ", ".join(c["name"] for c in calendars if c["name"]) or "none"
        return "", "", f"I couldn't find a calendar called “{name}” that I can add to (yours: {names}), " \
                       f"so this goes in your main calendar."

    async def from_chat(self, prompt: str) -> list[dict]:
        """'add X on <date> at <time> to my calendar' → proposals. Read by script first; the model only helps
        when the script can't find a date."""
        now = datetime.now(self.settings.tz)
        calendar_id, calendar_label, self.last_calendar_problem = await self.find_calendar(calendar_name(prompt))
        if calendar_label:
            diag.event("events", f"target calendar: {calendar_label}", calendar_id=calendar_id)
        scripted = parse_event_request(prompt, now)
        if scripted is not None:
            diag.event("events", f"calendar request understood by script: {scripted.title} @ {scripted.start}",
                       end=scripted.end, all_day=scripted.all_day)
            candidates = [scripted]
        else:
            diag.debug("events", "script couldn't find a date — asking the model")
            raw = await self.llm.chat([
                {"role": "system", "content": CHAT_PROMPT},
                {"role": "user", "content": f"Current date/time: {now:%A %d %B %Y %H:%M}\nMessage: {prompt}"},
            ], json_mode=True)
            candidates = validate_llm_events(raw, self.settings.tz, now, min_confidence=0.3)
            diag.event("events", f"model read the calendar request: {len(candidates)} event(s)", raw=raw[:800])
        proposals = []
        for candidate in candidates:
            candidate.source = "chat"
            if not self._is_future(candidate):
                diag.debug("events", f"not added — {candidate.start} is in the past")
                continue
            print_ = fingerprint(candidate.title, candidate.start) + f":{int(time.time())}"
            cursor = self.db.execute(
                "INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, location, notes, "
                "confidence, status, calendar_id, calendar_name) VALUES (?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (time.time(), print_, candidate.title, candidate.start, candidate.end, int(candidate.all_day),
                 candidate.location, candidate.notes, candidate.confidence, calendar_id, calendar_label),
            )
            proposals.append(self.get(cursor.lastrowid))
        return proposals
