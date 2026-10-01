"""Reminders and scheduled Home Assistant actions.

- Reminders are only notifications to you, so they are scheduled straight away.
- Home Assistant actions are proposals: they run (now or at the set time) only after you confirm.
- Everything open is mirrored to Jarvis/Reminders.md in Obsidian Tasks format; ticking an item
  there cancels it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

from .. import diag
from ..config import Settings
from ..db import Database
from ..extract.when import describe, next_occurrence, parse_when
from ..ha import HAError, HomeAssistant, parse_command
from ..notify import Notifier
from ..vault.client import ObsidianVault, VaultError
from ..vault.markdown import one_line

log = logging.getLogger(__name__)
REMIND = re.compile(r"^\s*(?:please\s+)?(?:remind\s+me|set\s+(?:a\s+)?reminder|reminder)\b\s*(?:to|about|that|for|of)?\s*(.*)$",
                    re.I | re.S)
HA_MISSED_GRACE = 30 * 60


def reminder_text(prompt: str) -> str | None:
    match = REMIND.match(prompt)
    return match.group(1).strip() if match else None


class Scheduler:
    def __init__(self, settings: Settings, db: Database, ha: HomeAssistant, notifier: Notifier,
                 vault: ObsidianVault) -> None:
        self.settings = settings
        self.db = db
        self.ha = ha
        self.notifier = notifier
        self.vault = vault

    def now(self) -> datetime:
        return datetime.now(self.settings.tz)

    # ---------------------------------------------------------------- creating
    def add_reminder(self, text: str, source: str = "chat") -> dict:
        """Returns the created row, or {'error': ...} if no time could be found."""
        when = parse_when(text, self.now())
        what = when.rest or text
        what = re.sub(r"^(to|about|that|of)\s+", "", what, flags=re.I).strip() or text
        if when.at is None:
            diag.warning("reminders", "no time found in reminder", text=text)
            return {"error": "no-time", "text": what}
        diag.event("reminders", f"parsed “{text}”", at=when.at.isoformat(), repeat=when.repeat, what=what)
        cursor = self.db.execute(
            "INSERT INTO scheduled (created, kind, text, due, repeat, status, source) VALUES (?, 'reminder', ?, ?, ?, 'scheduled', ?)",
            (time.time(), what[:300], when.at.timestamp(), when.repeat, source))
        self.db.queue_note("reminders", "all")
        return self.get(cursor.lastrowid)

    async def propose_home_action(self, prompt: str) -> dict | None:
        """Parse a home command (optionally timed). Returns a proposal row, None if not a command,
        or {'error': ...} when it is a command that can't be resolved."""
        when = parse_when(prompt, self.now())
        command = parse_command(when.rest if when.at else prompt)
        if command is None:
            return None
        if not self.ha.configured:
            if command.verb in {"on", "off", "lock", "unlock"}:
                return {"error": "Home Assistant isn't connected yet (set HA_URL and HA_TOKEN)."}
            return None
        diag.event("home", f"command parsed: {command.verb} “{command.target}”", value=command.value,
                   when=when.at.isoformat() if when.at else "now", repeat=when.repeat)
        try:
            action = await self.ha.resolve(command)
            diag.event("home", f"resolved to {action.domain}.{action.service}", entities=action.entity_ids,
                       data=action.data)
        except HAError as error:
            diag.warning("home", f"could not resolve “{command.target}”: {error}")
            if command.verb in {"open", "close", "activate", "set"} and "couldn't find" in str(error):
                return None  # probably not a home command ("open my notes…"); let the normal chat handle it
            return {"error": str(error)}
        cursor = self.db.execute(
            "INSERT INTO scheduled (created, kind, text, due, repeat, payload, status, source) "
            "VALUES (?, 'ha', ?, ?, ?, ?, 'proposed', 'chat')",
            (time.time(), action.description, when.at.timestamp() if when.at else None, when.repeat,
             json.dumps(action.payload())))
        return self.get(cursor.lastrowid)

    # ---------------------------------------------------------------- reading
    def get(self, item_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM scheduled WHERE id = ?", (item_id,))
        return self.present(row) if row else None

    def present(self, row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item["payload"] or "{}")
        at = datetime.fromtimestamp(item["due"], self.settings.tz) if item["due"] else None
        item["when"] = describe(at, item["repeat"], self.now())
        return item

    def list(self, statuses: tuple[str, ...] = ("proposed", "scheduled"), limit: int = 100) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.all(f"SELECT * FROM scheduled WHERE status IN ({marks}) ORDER BY due IS NOT NULL, due LIMIT ?",
                           (*statuses, limit))
        return [self.present(r) for r in rows]

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self.db.all("SELECT * FROM scheduled WHERE status IN ('done', 'failed', 'cancelled') "
                           "ORDER BY COALESCE(last_run, created) DESC LIMIT ?", (limit,))
        return [self.present(r) for r in rows]

    # ---------------------------------------------------------------- decisions
    async def confirm(self, item_id: int) -> dict:
        row = self.db.one("SELECT * FROM scheduled WHERE id = ?", (item_id,))
        if row is None or row["status"] != "proposed":
            raise HAError("That action isn't waiting for confirmation.")
        if row["due"] is None:
            self.db.execute("UPDATE scheduled SET status = 'scheduled' WHERE id = ?", (item_id,))
            await self.execute(self.db.one("SELECT * FROM scheduled WHERE id = ?", (item_id,)))
        else:
            self.db.execute("UPDATE scheduled SET status = 'scheduled' WHERE id = ?", (item_id,))
        self.db.queue_note("reminders", "all")
        return self.get(item_id)

    def cancel(self, item_id: int) -> None:
        self.db.execute("UPDATE scheduled SET status = CASE status WHEN 'proposed' THEN 'dismissed' ELSE 'cancelled' END, "
                        "decided = ? WHERE id = ? AND status IN ('proposed', 'scheduled')", (time.time(), item_id))
        self.db.queue_note("reminders", "all")

    # ---------------------------------------------------------------- running
    async def run_due(self) -> dict:
        now = time.time()
        rows = self.db.all("SELECT * FROM scheduled WHERE status = 'scheduled' AND due IS NOT NULL AND due <= ?", (now,))
        ran = 0
        for row in rows:
            await self.execute(row)
            ran += 1
        return {"ran": ran}

    async def execute(self, row) -> None:
        now_ts = time.time()
        due = row["due"] or now_ts
        late = now_ts - due
        result, ok = "", True
        if row["kind"] == "reminder":
            suffix = f" (late — due {datetime.fromtimestamp(due, self.settings.tz):%H:%M})" if late > 600 else ""
            await self.notifier.notify("Reminder", row["text"] + suffix, 4,
                                       self.settings.public_url.rstrip("/") + "/#plan",
                                       dedupe=f"reminder:{row['id']}:{int(due)}", tags="alarm_clock",
                                       category="reminders")
            result = "reminded"
            diag.event("reminders", f"reminded: {row['text']}", late_seconds=round(late))
            self.journal(f"Reminder: {row['text']}")
        else:
            payload = json.loads(row["payload"])
            if late > HA_MISSED_GRACE:
                ok, result = False, f"missed (Jarvis was offline {int(late // 60)} min past the time)"
            else:
                try:
                    await self.ha.call(payload["domain"], payload["service"], payload["entity_ids"], payload.get("data"))
                    result = "done"
                except HAError as error:
                    ok, result = False, str(error)
            diag.event("home", f"{'ran' if ok else 'FAILED'}: {row['text']}", level=diag.INFO if ok else diag.ERROR,
                       payload=payload, result=result, late_seconds=round(late))
            title = f"{'Done' if ok else 'Failed'}: {row['text']}"
            await self.notifier.notify(title, result if not ok else "Home Assistant confirmed the action.",
                                       2 if ok else 4, self.settings.public_url.rstrip("/") + "/#plan",
                                       dedupe=f"ha:{row['id']}:{int(due)}", tags="house" if ok else "warning",
                                       category="home")
            self.journal(f"Home: {row['text']} — {result}")
        next_due = None
        if row["repeat"] and row["due"]:
            at = datetime.fromtimestamp(row["due"], self.settings.tz)
            while at is not None and at.timestamp() <= now_ts:
                at = next_occurrence(at, row["repeat"])
            next_due = at.timestamp() if at else None
        if next_due:
            self.db.execute("UPDATE scheduled SET due = ?, last_run = ?, result = ? WHERE id = ?",
                            (next_due, now_ts, result, row["id"]))
        else:
            self.db.execute("UPDATE scheduled SET status = ?, last_run = ?, result = ? WHERE id = ?",
                            ("done" if ok else "failed", now_ts, result, row["id"]))
        self.db.queue_note("reminders", "all")

    def journal(self, text: str) -> None:
        now = self.now()
        self.db.execute("INSERT OR REPLACE INTO journal (day, kind, ref, ts, text) VALUES (?, 'scheduled', ?, ?, ?)",
                        (f"{now:%Y-%m-%d}", f"{now.timestamp():.3f}", now.timestamp(), one_line(text, 200)))
        self.db.queue_note("journal", f"{now:%Y-%m-%d}")

    async def sync_ticks(self) -> dict:
        """Ticking an open item in Jarvis/Reminders.md cancels it."""
        try:
            text = await self.vault.get_text("Jarvis/Reminders.md")
        except VaultError:
            return {"skipped": "vault offline"}
        cancelled = 0
        for match in re.finditer(r"^- \[[xX]\] .*\^r(\d+)\s*$", text or "", re.M):
            item_id = int(match.group(1))
            row = self.db.one("SELECT status FROM scheduled WHERE id = ?", (item_id,))
            if row and row["status"] in {"scheduled", "proposed"}:
                self.cancel(item_id)
                cancelled += 1
        return {"cancelled": cancelled}

    def due_between(self, start: datetime, end: datetime) -> list[dict]:
        rows = self.db.all("SELECT * FROM scheduled WHERE status IN ('scheduled', 'proposed') AND due >= ? AND due < ? "
                           "ORDER BY due", (start.timestamp(), end.timestamp()))
        return [self.present(r) for r in rows]

