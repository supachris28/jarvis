"""Triggers from Home Assistant: the morning brief when you come downstairs, a summary when you get home, the
evening preview on demand.

Home Assistant calls POST /api/hook/<event> with the hook token (Status → Home Assistant triggers). Each event
returns a short spoken summary ("speech") that an automation can read out on a speaker. Events:
  morning  — sends today's brief now, unless it has already gone out today (BRIEF_TIME stays as the fallback)
  home     — "welcome home": the rest of today, parcels delivered or waiting to collect, things due tomorrow
  evening  — sends the evening preview now, unless already sent today
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from datetime import date, datetime, timedelta

from . import diag
from .notify import push_text

EVENTS = ("morning", "home", "evening")


def spoken(lines: list[str]) -> str:
    """Brief lines → sentences for a speaker (no Markdown, no emoji, no links)."""
    text = " ".join(push_text(line).strip(" -") for line in lines if line.strip())
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class Hooks:
    def __init__(self, services) -> None:
        self.services = services
        self._last: dict[str, float] = {}

    # ---------------------------------------------------------------- token
    @property
    def token(self) -> str:
        db = self.services.db
        token = db.get("hooks.token")
        if not token:
            token = secrets.token_urlsafe(24)
            db.set("hooks.token", token)
        return token

    def new_token(self) -> str:
        self.services.db.set("hooks.token", secrets.token_urlsafe(24))
        return self.token

    def check(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.token.encode())

    # ---------------------------------------------------------------- events
    async def fire(self, event: str) -> dict:
        if event not in EVENTS:
            return {"error": f"unknown event (use {', '.join(EVENTS)})"}
        now = time.time()
        if now - self._last.get(event, 0) < 30:
            return {"skipped": "already handled in the last 30 seconds", "speech": ""}
        self._last[event] = now
        diag.event("hook", f"Home Assistant: {event}")
        return await getattr(self, f"on_{event}")()

    async def on_morning(self) -> dict:
        s = self.services
        tz = s.settings.tz
        today = datetime.now(tz).date()
        sent_already = bool(s.db.get(f"brief.sent.{today}"))
        result = "already sent today" if sent_already else await s.brief.run(force=True)
        return {"brief": result, "speech": self.morning_speech(today)}

    def morning_speech(self, today: date) -> str:
        s = self.services
        events = s.brief.events_on(today)
        parts = ["Good morning."]
        if events:
            first = push_text(events[0]).split(" (")[0]
            parts.append(f"You have {plural(len(events), 'thing')} in the calendar today, starting with {first}.")
        else:
            parts.append("Nothing in the calendar today.")
        if s.deliveries is not None:
            arriving = s.deliveries.summary_lines(only_today=True)
            waiting = s.deliveries.ready_lines()
            if arriving:
                parts.append(f"{plural(len(arriving), 'parcel')} on the way today.")
            if waiting:
                parts.append(f"{plural(len(waiting), 'parcel')} ready to collect.")
        if s.deadlines is not None:
            soon = s.deadlines.upcoming(2)
            if soon:
                parts.append("Due soon: " + "; ".join(f"{d['label']} {d['away']}, {d['org']}" for d in soon[:3]) + ".")
        return spoken(parts)

    async def on_home(self) -> dict:
        s = self.services
        tz = s.settings.tz
        now = datetime.now(tz)
        today = now.date()
        lines, speech = [], ["Welcome home."]
        later = [line for line in s.brief.events_on(today)
                 if re.match(r"\d{2}:\d{2}", line) and line[:5] >= f"{now:%H:%M}"]
        if later:
            lines.append("**Still to come today**\n" + "\n".join(f"- {line}" for line in later))
            speech.append(f"Still today: {push_text(later[0]).split(' (')[0]}.")
        end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tz)
        due = s.scheduler.due_between(now, end)
        if due:
            lines.append("**Reminders later**\n" + "\n".join(
                f"- {datetime.fromtimestamp(r['due'], tz):%H:%M} — {r['text']}" for r in due))
            speech.append(f"{plural(len(due), 'reminder')} later.")
        if s.deliveries is not None:
            delivered = [d for d in s.deliveries.active()
                         if d["status"] == "delivered" and d.get("delivered_at")
                         and datetime.fromtimestamp(d["delivered_at"], tz).date() == today]
            if delivered:
                lines.append("**Delivered today**\n" + "\n".join(
                    f"- ✅ {d['name']}{' — ' + d['status_text'] if d['status_text'] else ''}" for d in delivered))
                speech.append(f"{plural(len(delivered), 'parcel')} delivered today — check the porch.")
            waiting = s.deliveries.ready_lines()
            if waiting:
                lines.append("**Ready to collect**\n" + "\n".join(f"- {line}" for line in waiting))
                speech.append(f"{plural(len(waiting), 'parcel')} waiting to be collected.")
        if s.deadlines is not None:
            soon = [d for d in s.deadlines.upcoming(1)]
            if soon:
                lines.append("**Due by tomorrow**\n" + "\n".join(
                    f"- {d['icon']} {d['label']} {d['away']}: {d['title']} · {d['org']}" for d in soon))
                speech.append("Due by tomorrow: " + "; ".join(f"{d['label']}, {d['org']}" for d in soon[:3]) + ".")
        tomorrow = s.brief.events_on(today + timedelta(days=1))
        timed = [line for line in tomorrow if re.match(r"\d{2}:\d{2}", line)]
        if timed and timed[0][:5] < "09:00":
            lines.append(f"**Early start tomorrow**\n- {timed[0]}")
            speech.append(f"Early start tomorrow at {timed[0][:5]}.")
        if not lines:
            lines.append("Nothing else on today.")
        text = "**Welcome home**\n\n" + "\n\n".join(lines)
        await s.notifier.notify("Welcome home", text.split("\n", 2)[-1], 3,
                                s.settings.public_url.rstrip("/") + "/#chat",
                                dedupe=f"home:{today}:{now.hour}", tags="house", category="welcome")
        if s.notifier.in_chat("welcome"):
            s.db.execute("INSERT INTO chat_messages (ts, role, content, trace) VALUES (?, 'activity', ?, ?)",
                         (time.time(), text, diag.current_trace_id()))
        return {"text": text, "speech": spoken(speech)}

    async def on_evening(self) -> dict:
        s = self.services
        today = datetime.now(s.settings.tz).date()
        result = "already sent today" if s.db.get(f"evening.sent.{today}") else await s.brief.run_evening(force=True)
        text = await s.brief.build_evening(today)
        lines = [line for line in text.splitlines() if line.startswith("- ")][:4]
        return {"evening": result, "speech": spoken(["Tomorrow:"] + lines)}
