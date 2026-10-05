"""Notifications through ntfy, with dedupe, quiet hours and a rate cap."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, time as dtime

import httpx

from . import http

from . import diag

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)


def push_text(text: str) -> str:
    """Phone notifications show text as-is (the ntfy Android app doesn't render Markdown), so links and
    formatting become plain words: [[Note|Name]] → Name, [track](https://…) → track, **bold** → bold."""
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).rsplit("/", 1)[-1], text)
    text = re.sub(r"\s*—?\s*\[(track|open|link)\]\([^)]*\)", "", text, flags=re.I)  # bare link words add nothing
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def in_quiet_hours(now: datetime, spec: str) -> bool:
    try:
        start_s, end_s = spec.split("-")
        start = dtime.fromisoformat(start_s.strip())
        end = dtime.fromisoformat(end_s.strip())
    except ValueError:
        return False
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


# Each notification has a category you can switch off, or let through quiet hours, in Alerts → Settings.
CATEGORIES = {
    "brief":     {"label": "Morning brief",                   "on": True,  "quiet": False, "chat": True},
    "weekly":    {"label": "Weekly review (Sunday evening)",  "on": True,  "quiet": False, "chat": True},
    "people":    {"label": "Before you meet someone",          "on": True,  "quiet": True,  "chat": True},
    "evening":   {"label": "Evening preview of tomorrow",     "on": True,  "quiet": False, "chat": True},
    "reminders": {"label": "Your reminders",                  "on": True,  "quiet": False, "chat": False},
    "calendar":  {"label": "Upcoming calendar events",        "on": True,  "quiet": True,  "chat": False},
    "events":    {"label": "Events found in email",           "on": True,  "quiet": True,  "chat": False},
    "email":     {"label": "Important email",                 "on": True,  "quiet": True,  "chat": False},
    "home":      {"label": "Home actions",                    "on": True,  "quiet": True,  "chat": False},
    "saves":     {"label": "Notes saved to your vault",       "on": False, "quiet": True,  "chat": True},
    "deliveries": {"label": "Delivery updates",               "on": True,  "quiet": True,  "chat": False},
    "welcome":   {"label": "Welcome home summary (Home Assistant)", "on": True, "quiet": True, "chat": True},
    "birthdays": {"label": "Birthday heads-up (a week before, with gift ideas)", "on": True, "quiet": True,
                  "chat": True},
    "collections": {"label": "Ready to collect (lockers, shops, Click & Collect)", "on": True, "quiet": True,
                    "chat": True},
    "deadlines": {"label": "Renewals and deadlines",          "on": True,  "quiet": True,  "chat": False},
    "followups": {"label": "No reply yet to your emails",     "on": False, "quiet": True,  "chat": False},
    "system":    {"label": "Jarvis problems (e.g. Google sign-in)", "on": True, "quiet": True, "chat": False},
}
# "chat": also post it in the chat timeline. The brief and vault save reports post their own (fuller) messages there.
CHAT_SELF_POSTED = {"brief", "evening", "saves", "welcome", "weekly"}
# "quiet": True = held during quiet hours (and counted in the hourly cap); False = always delivered at once.


class Notifier:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    @property
    def configured(self) -> bool:
        return bool(self.settings.ntfy_url and self.settings.ntfy_topic)

    def status(self) -> dict:
        if not self.configured:
            return {"ok": False, "detail": "not configured (NTFY_URL / NTFY_TOPIC)"}
        return {"ok": True, "detail": f"{self.settings.ntfy_url.rstrip('/')}/{self.settings.ntfy_topic}"}

    def preferences(self) -> dict[str, dict]:
        saved = self.db.get("notify.categories") or {}
        prefs = {}
        for key, default in CATEGORIES.items():
            mine = saved.get(key) if isinstance(saved.get(key), dict) else {}
            prefs[key] = {"label": default["label"], "on": bool(mine.get("on", default["on"])),
                          "quiet": bool(mine.get("quiet", default["quiet"])),
                          "chat": bool(mine.get("chat", default["chat"]))}
        return prefs

    def set_preferences(self, changes: dict) -> dict[str, dict]:
        saved = self.db.get("notify.categories") or {}
        for key, value in (changes or {}).items():
            if key in CATEGORIES and isinstance(value, dict):
                saved[key] = {**(saved.get(key) if isinstance(saved.get(key), dict) else {}),
                              **{k: bool(value[k]) for k in ("on", "quiet", "chat") if k in value}}
        self.db.set("notify.categories", saved)
        return self.preferences()

    def in_chat(self, category: str) -> bool:
        return self.preferences().get(category, {"chat": False})["chat"]

    def enabled(self, category: str) -> bool:
        return self.preferences().get(category, {"on": True})["on"]

    async def notify(self, title: str, message: str, priority: int = 3, url: str = "", dedupe: str | None = None,
                     tags: str = "", category: str = "system") -> str:
        """Queue and (if allowed now) send. Returns the resulting status."""
        pref = self.preferences().get(category, {"on": True, "quiet": True, "chat": False})
        # the brief and save reports post their own chat messages, so their chat tick doesn't need a row here
        to_chat = pref["chat"] and category not in CHAT_SELF_POSTED
        if not pref["on"] and not to_chat:
            diag.debug("notify", f"not sent — “{CATEGORIES.get(category, {}).get('label', category)}” is switched off: "
                                 f"{title}")
            return "off"
        try:
            cursor = self.db.execute(
                "INSERT INTO notifications (ts, dedupe, title, message, priority, url, status) VALUES (?, ?, ?, ?, ?, ?, 'queued')",
                (time.time(), dedupe, title, message, priority, url or self.settings.public_url),
            )
        except sqlite3.IntegrityError:
            failed = self.db.one("SELECT id FROM notifications WHERE dedupe = ? AND status = 'failed'", (dedupe,))
            if failed is None:
                diag.debug("notify", f"duplicate suppressed: {title}", dedupe=dedupe)
                return "duplicate"
            # the earlier attempt failed (ntfy down): this is a retry, not a duplicate
            self.db.execute("UPDATE notifications SET status = 'queued', error = '', ts = ? WHERE id = ?",
                            (time.time(), failed["id"]))
            cursor = None
        note_id = cursor.lastrowid if cursor is not None else failed["id"]
        if to_chat and cursor is not None:
            self._post_to_chat(title, message, url)
        if not pref["on"]:  # chat only: kept (status 'off') so the same alert isn't posted twice
            self.db.execute("UPDATE notifications SET status = 'off' WHERE id = ?", (note_id,))
            return "off"
        now = datetime.now(self.settings.tz)
        recent = self.db.one("SELECT COUNT(*) AS n FROM notifications WHERE status = 'sent' AND ts > ?",
                             (time.time() - 3600,))["n"]
        if not pref["quiet"]:  # e.g. the morning brief: delivered at once, whatever the time or cap
            return await self._send(note_id, title, message, priority, url, tags)
        if (priority < 5 and in_quiet_hours(now, self.settings.notify_quiet_hours)) or \
                recent >= self.settings.notify_max_per_hour:
            self.db.execute("UPDATE notifications SET status = 'held' WHERE id = ?", (note_id,))
            reason = "quiet hours" if in_quiet_hours(now, self.settings.notify_quiet_hours) and priority < 5 \
                else f"rate cap ({self.settings.notify_max_per_hour}/hour)"
            diag.event("notify", f"held ({reason}): {title}", priority=priority)
            return "held"
        return await self._send(note_id, title, message, priority, url, tags)

    def _post_to_chat(self, title: str, message: str, url: str) -> None:
        link = ""
        if url:
            local = self.settings.public_url.rstrip("/")
            target = url[len(local):] if local and url.startswith(local + "/") else url
            link = f"\n[Open]({target})" if target.startswith(("http://", "https://", "/#")) else ""
        self.db.execute("INSERT INTO chat_messages (ts, role, content, trace) VALUES (?, 'activity', ?, ?)",
                        (time.time(), f"🔔 **{title}**\n{message}{link}", diag.current_trace_id()))

    async def _send(self, note_id: int, title: str, message: str, priority: int, url: str, tags: str = "") -> str:
        if not self.configured:
            self.db.execute("UPDATE notifications SET status = 'logged' WHERE id = ?", (note_id,))
            diag.debug("notify", f"ntfy not configured — only logged: {title}")
            return "logged"
        body = {"topic": self.settings.ntfy_topic, "title": push_text(title), "message": push_text(message),
                "priority": max(1, min(5, priority)), "click": url or self.settings.public_url,
                "markdown": False}
        if tags:
            body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        auth = {"Authorization": f"Bearer {self.settings.ntfy_token}"} if self.settings.ntfy_token else {}
        try:
            client = http.shared(timeout=15)
            # JSON publishing handles UTF-8 titles safely.
            response = await client.post(self.settings.ntfy_url.rstrip("/"), json=body, headers=auth)
            response.raise_for_status()
        except httpx.HTTPError as error:
            self.db.execute("UPDATE notifications SET status = 'failed', error = ? WHERE id = ?",
                            (f"{type(error).__name__}: {error}"[:300], note_id))
            diag.warning("notify", f"ntfy publish failed: {title}", error=f"{type(error).__name__}: {error}")
            return "failed"
        self.db.execute("UPDATE notifications SET status = 'sent' WHERE id = ?", (note_id,))
        diag.event("notify", f"sent (p{priority}): {title}")
        return "sent"

    async def release_held(self) -> int:
        """After quiet hours, send one digest of everything that was held."""
        now = datetime.now(self.settings.tz)
        if in_quiet_hours(now, self.settings.notify_quiet_hours):
            return 0
        held = self.db.all("SELECT * FROM notifications WHERE status = 'held' ORDER BY ts")
        if not held:
            return 0
        recent = self.db.one("SELECT COUNT(*) AS n FROM notifications WHERE status = 'sent' AND ts > ?",
                             (time.time() - 3600,))["n"]
        if recent >= self.settings.notify_max_per_hour:
            return 0
        ids = [row["id"] for row in held]
        lines = [f"- **{row['title']}** — {row['message'][:120]}" for row in held[:20]]
        if len(held) > 20:
            lines.append(f"- …and {len(held) - 20} more")
        self.db.execute(f"UPDATE notifications SET status = 'digested' WHERE id IN ({','.join('?' * len(ids))})", ids)
        cursor = self.db.execute(
            "INSERT INTO notifications (ts, title, message, priority, url, status) VALUES (?, ?, ?, 3, ?, 'queued')",
            (time.time(), f"{len(held)} held notification(s)", "\n".join(lines), self.settings.public_url),
        )
        await self._send(cursor.lastrowid, f"{len(held)} held notification(s)", "\n".join(lines), 3,
                         self.settings.public_url.rstrip("/") + "/#notifications")
        return len(held)
