"""Tier 0 Gmail pipeline: collect → classify (rules) → store → queue vault notes → notify.

No LLM is used here. Bulk mail is counted but not written to the vault.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from .. import diag
from ..config import Settings
from ..db import Database
from ..google.gmail import Gmail, ParsedMessage, app_email_url, parse_message
from ..google.oauth import GoogleError
from ..notify import Notifier
from ..vault.markdown import link, one_line, safe_name
from ..vault.writer import VaultWriter

log = logging.getLogger(__name__)
BACKFILL_LIMIT = 2000


class GmailPipeline:
    def __init__(self, settings: Settings, db: Database, gmail: Gmail, writer: VaultWriter, notifier: Notifier) -> None:
        self.settings = settings
        self.db = db
        self.gmail = gmail
        self.writer = writer
        self.notifier = notifier
        self.finder = None  # EventFinder, attached by Services
        self.deliveries = None  # Deliveries, attached by Services

    async def run(self) -> dict:
        started = time.time()
        history_id = self.db.get("gmail.history_id")
        backfill = history_id is None
        if backfill:
            profile = await self.gmail.profile()
            self.db.set("gmail.me", profile.get("emailAddress", "").casefold())
            latest = str(profile.get("historyId"))
            ids = await self.gmail.list_message_ids(f"newer_than:{self.settings.backfill_days}d", BACKFILL_LIMIT)
        else:
            if not self.db.get("gmail.me"):  # needed for links that open the right account
                self.db.set("gmail.me", str((await self.gmail.profile()).get("emailAddress", "")).casefold())
            try:
                ids, latest = await self.gmail.history(str(history_id))
            except GoogleError as error:
                if getattr(error, "status", None) != 404:
                    raise
                diag.warning("gmail", "history id expired — falling back to a date search", history_id=history_id)
                # history id expired (PC/server was off a long time): fall back to a date query
                since = int(self.db.get("gmail.last_run", started - 86400)) - 3600
                ids = await self.gmail.list_message_ids(f"after:{since}", BACKFILL_LIMIT)
                latest = str((await self.gmail.profile()).get("historyId"))

        known: set[str] = set()
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):  # SQLite caps bound parameters
            known.update(r["message_id"] for r in self.db.all(
                f"SELECT message_id FROM emails WHERE message_id IN ({','.join('?' * len(chunk))})", chunk))
        new_ids = [i for i in ids if i not in known]
        semaphore = asyncio.Semaphore(5)

        async def fetch(message_id: str) -> dict | None:
            async with semaphore:
                try:
                    return await self.gmail.message(message_id)
                except GoogleError as error:
                    if getattr(error, "status", None) == 404:
                        return None
                    raise

        stored = bulk = notified = proposals = 0
        for chunk_start in range(0, len(new_ids), 50):
            chunk = new_ids[chunk_start:chunk_start + 50]
            for raw in await asyncio.gather(*(fetch(i) for i in chunk)):
                if raw is None or "DRAFT" in (raw.get("labelIds") or []):
                    continue
                message = parse_message(raw, self.settings.email_body_limit)
                diag.debug("gmail", f"{'bulk' if message.bulk else 'kept'}: {message.subject[:80]}",
                           id=message.message_id, sender=message.from_addr, labels=message.labels,
                           reason=message.bulk_reason or ("sent by you" if message.outgoing else "personal/transactional"),
                           ics=len(message.ics_inline) + len(message.ics_attachment_ids))
                self.store(message)
                stored += 1
                if self.finder is not None:
                    try:
                        proposals += await self.finder.on_message(message, notify=not backfill)
                    except Exception as error:  # noqa: BLE001 — one odd email must not stop the whole run
                        diag.error("events", f"event check failed for “{message.subject[:60]}”: "
                                   f"{type(error).__name__}: {error}", error, id=message.message_id)
                if self.deliveries is not None:
                    try:
                        self.deliveries.on_message(message)
                    except Exception as error:  # noqa: BLE001
                        diag.error("deliveries", f"delivery check failed for “{message.subject[:60]}”: "
                                   f"{type(error).__name__}: {error}", error, id=message.message_id)
                if message.bulk:
                    bulk += 1
                    continue
                self.link_people_and_journal(message)
                if not backfill and await self.maybe_notify(message):
                    notified += 1

        if self.deliveries is not None:
            if backfill:
                self.deliveries._pending.clear()  # don't announce every old parcel on the first run
            else:
                await self.deliveries.flush_notifications()
        self.db.set("gmail.history_id", latest)
        self.db.set("gmail.last_run", started)
        if self.finder is not None:
            if backfill and proposals:
                self.finder._pending_notifications.clear()
                await self.notifier.notify(
                    f"{proposals} possible event(s) found in recent email",
                    "Review them in Jarvis → Events before they are added to your calendar.",
                    3, self.settings.public_url.rstrip("/") + "/#events", dedupe=None, tags="calendar",
                    category="events")
            else:
                await self.finder.flush_notifications()
        return {"new": stored, "bulk_skipped": bulk, "notified": notified, "event_proposals": proposals,
                "backfill": backfill}

    # storage -----------------------------------------------------------------
    def store(self, message: ParsedMessage) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO emails (message_id, thread_id, ts, from_addr, from_name, to_addrs, subject, labels, "
            "bulk, outgoing, snippet, body, attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.thread_id, message.ts, message.from_addr, message.from_name,
             json.dumps(message.to), message.subject, json.dumps(message.labels), int(message.bulk),
             int(message.outgoing), message.snippet, "" if message.bulk else message.body,
             json.dumps(message.attachments)),
        )
        if message.bulk:
            return
        thread = self.db.one("SELECT * FROM threads WHERE thread_id = ?", (message.thread_id,))
        if thread is None:
            when = datetime.fromtimestamp(message.ts, self.settings.tz)
            subject = message.subject or "(no subject)"
            path = (f"Sources/Email/{when:%Y}/{when:%m}/{when:%Y-%m-%d} {safe_name(subject, 70)} "
                    f"({message.thread_id[-6:]}).md")
            self.db.execute("INSERT INTO threads (thread_id, path, subject, first_ts) VALUES (?, ?, ?, ?)",
                            (message.thread_id, path, subject, message.ts))
        self.db.queue_note("thread", message.thread_id)

    def link_people_and_journal(self, message: ParsedMessage) -> None:
        me = self.db.get("gmail.me", "")
        if message.outgoing:
            for addr, name in message.to:
                if addr != me:
                    self.writer.ensure_person(addr, name, create=True)
        else:
            self.writer.ensure_person(message.from_addr, message.from_name, create=message.personal)
        thread = self.db.one("SELECT path FROM threads WHERE thread_id = ?", (message.thread_id,))
        subject_link = link(thread["path"], one_line(message.subject, 80) or "email")
        if message.outgoing:
            names = [self._who(a, n) for a, n in message.to if a != me][:3]
            text = f"Email to {', '.join(names) or 'someone'}: {subject_link}"
        else:
            text = f"Email from {self._who(message.from_addr, message.from_name)}: {subject_link}"
        day = datetime.fromtimestamp(message.ts, self.settings.tz).strftime("%Y-%m-%d")
        self.db.execute(
            "INSERT OR REPLACE INTO journal (day, kind, ref, ts, text) VALUES (?, 'email', ?, ?, ?)",
            (day, message.message_id, message.ts, text),
        )
        self.db.queue_note("journal", day)

    def _who(self, addr: str, name: str) -> str:
        path = self.writer.person_path(addr)
        return link(path, name or None) if path else (name or addr)

    # notifications -------------------------------------------------------------
    def importance(self, message: ParsedMessage) -> int:
        if message.outgoing or message.bulk or "INBOX" not in message.labels:
            return 0
        if time.time() - message.ts > 86400:
            return 0
        sender = message.from_addr
        domain = sender.rsplit("@", 1)[-1]
        if any(v == sender or v.lstrip("@") == domain for v in self.settings.notify_vip_senders):
            return 4
        haystack = f"{message.subject} {message.snippet}".casefold()
        if any(k in haystack for k in self.settings.notify_keywords):
            return 3
        if "IMPORTANT" in message.labels and message.personal and self.writer.person_path(sender):
            return 3
        return 0

    async def maybe_notify(self, message: ParsedMessage) -> bool:
        priority = self.importance(message)
        if not priority:
            return False
        sender = message.from_name or message.from_addr
        status = await self.notifier.notify(
            title=f"Email from {sender}",
            message=f"**{message.subject or '(no subject)'}**\n{one_line(message.snippet, 200)}",
            priority=priority,
            url=app_email_url(message.thread_id, self.settings.public_url),
            dedupe=f"email:{message.message_id}", category="email",
            tags="envelope",
        )
        return status in {"sent", "held", "logged"}
