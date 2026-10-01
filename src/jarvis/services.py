"""Wires all components together and runs the background jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .assistant.core import Assistant
from .config import Settings
from . import diag
from .db import Database
from .google.calendar import Calendar
from .google.gmail import Gmail
from .google.oauth import GoogleAuthRequired, GoogleError, GoogleOAuth
from .llm import Ollama
from .mcp import MCPClient
from .notify import Notifier
from .pipelines.calendar import CalendarPipeline
from .pipelines.brief import Brief
from .pipelines.contacts import ContactsPipeline
from .pipelines.deliveries import Deliveries
from .pipelines.events import EventFinder
from .pipelines.scheduled import Scheduler
from .google.contacts import Contacts
from .ha import HomeAssistant
from .websearch import WebSearch
from .tts import Speech
from .pipelines.gmail import GmailPipeline
from .vault.client import ObsidianVault, VaultUnavailable
from .vault.files import FileVault
from .vault.webdav import NextcloudWriter
from .vault.writer import VaultWriter, format_saves

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    interval: int
    func: Callable[[], Awaitable[object]]
    description: str
    last_run: float = 0.0
    last_ok: bool | None = None
    last_result: str = ""
    running: bool = False
    last_trace: str = ""
    trigger: asyncio.Event = field(default_factory=asyncio.Event)


def quiet_result(result: object) -> bool:
    """True for background ticks that did nothing worth recording."""
    if result is None or result in ("not due", "nothing new", "off", "skipped: Google not connected") or result == 0:
        return True
    if isinstance(result, str) and result.endswith(" waiting"):
        return True
    if isinstance(result, dict):
        busy = {k: v for k, v in result.items() if k not in {"pending", "offline", "budget_left", "rows", "backfill"}
                and v not in (0, False, None, [], "")}
        return not busy
    return False


class Services:
    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.diag = diag.DiagStore(self.db, diag.DEBUG if settings.log_level == "debug" else diag.INFO,
                                   settings.log_retention_days)
        self.diag.services = {settings.ollama_url.rstrip("/"): "ollama", settings.obsidian_url.rstrip("/"): "obsidian",
                              settings.ha_url.rstrip("/"): "home-assistant", settings.ntfy_url.rstrip("/"): "ntfy",
                              settings.tts_url.rstrip("/"): "kokoro"}
        self.diag.service_map = lambda: {self.llm.base_url: "ollama", self.vault.base_url: "obsidian",
                                         self.ha.url: "home-assistant", settings.nextcloud_url.rstrip("/"): "nextcloud", settings.searxng_url.rstrip("/"): "searxng"}
        diag.install(self.diag)
        # notes that failed while something was misconfigured get another go after a restart
        self.db.execute("UPDATE outbox SET attempts = 0, last_error = '' WHERE attempts > 0")
        self.llm = Ollama(settings.ollama_url, settings.chat_model, settings.effective_router_model,
                          settings.llm_timeout, settings.ollama_keep_alive)
        if settings.vault_backend == "obsidian":
            self.vault = ObsidianVault(settings.obsidian_url, settings.obsidian_api_key, settings.obsidian_ca_cert,
                                       settings.obsidian_verify_tls)
        else:
            writer = None
            if settings.vault_write == "nextcloud" and settings.nextcloud_url:
                writer = NextcloudWriter(settings.nextcloud_url, settings.nextcloud_user,
                                         settings.nextcloud_app_password, settings.nextcloud_vault_dir,
                                         settings.nextcloud_verify_tls)
            self.vault = FileVault(settings.vault_path, self.db, settings.vault_file_mode, writer=writer)
        self.writer = VaultWriter(self.db, self.vault, settings.tz)
        self.oauth = GoogleOAuth(settings.google_client_id, settings.google_client_secret,
                                 settings.google_redirect_uri, settings.google_token_path)
        self.gmail = Gmail(self.oauth)
        self.calendar = Calendar(self.oauth)

        async def google_auth() -> dict:
            return {"Authorization": f"Bearer {await self.oauth.access_token()}"}

        self.drive = MCPClient("Google Drive MCP", "https://drivemcp.googleapis.com/mcp/v1", google_auth)
        self.notifier = Notifier(settings, self.db)
        self.gmail_pipeline = GmailPipeline(settings, self.db, self.gmail, self.writer, self.notifier)
        self.calendar_pipeline = CalendarPipeline(settings, self.db, self.calendar, self.writer, self.notifier)
        self.events = EventFinder(settings, self.db, self.gmail, self.calendar, self.llm, self.notifier)
        self.gmail_pipeline.finder = self.events
        self.deliveries = Deliveries(settings, self.db, self.notifier)
        self.gmail_pipeline.deliveries = self.deliveries
        self.deliveries.gmail = self.gmail
        self.deliveries.llm = self.llm
        try:
            self.events.cleanup_noise()  # suggestions from T&Cs/policy/offer emails made by older versions
        except Exception:  # noqa: BLE001 — never block startup on housekeeping
            log.exception("event suggestion cleanup failed")
        self.speech = Speech(settings)
        self.ha = HomeAssistant(settings.ha_url, settings.ha_token, settings.ha_verify_tls)
        self.web = WebSearch(settings, self.db)
        self.contacts_pipeline = ContactsPipeline(self.db, Contacts(self.oauth), self.writer)
        self.scheduler = Scheduler(settings, self.db, self.ha, self.notifier, self.vault)
        self.brief = Brief(settings, self.db, self.ha, self.scheduler, self.llm, self.notifier)
        self.brief.on_brief = self.post_brief
        self.writer.on_saved = self.announce_saves
        self.assistant = Assistant(settings, self.db, self.llm, self.vault, self.writer, self.gmail,
                                   self.calendar, self.drive, self.events, self.scheduler, self.brief, self.ha,
                                   self.web)
        self.assistant.deliveries = self.brief.deliveries = self.deliveries
        self.brief.birthday_source = self.assistant.birthdays  # contacts + People notes + vault mentions
        self.ha.alias_source = self.assistant.home_names  # "gas water heater" → water_heater.thermostat1
        self._learn_home_names_from_captures()
        self._apply_calendar_choice()
        self.jobs: dict[str, Job] = {}
        for job in (
            Job("model", 60, self.llm.warm, "Keep the model loaded on the PC's GPU (loads it when Ollama comes online)"),
            Job("people", 3600, self.writer.refresh_people, "Learn email addresses from People notes"),
            *([Job("index", 60, self.vault.refresh, "Index vault changes (including edits made via Nextcloud)")]
              if isinstance(self.vault, FileVault) else []),
            Job("contacts", 6 * 3600, self._google(self.contacts_pipeline.run), "Sync Google Contacts into People notes"),
            Job("gmail", settings.gmail_interval, self._google(self.gmail_pipeline.run), "Ingest new email"),
            Job("calendar", settings.calendar_interval, self._google(self.calendar_pipeline.run), "Ingest calendar"),
            Job("reminders", 60, self.calendar_pipeline.remind, "Event reminders"),
            Job("scheduled", 30, self.scheduler.run_due, "Your reminders and scheduled home actions"),
            Job("ticks", 300, self.scheduler.sync_ticks, "Cancel items ticked in Jarvis/Reminders.md"),
            Job("brief", 60, self.brief.run, f"Morning brief at {settings.brief_time}"),
            Job("events", 600, self.scan_events, "Find events in email (model, daily budget)"),
            Job("deliveries", 3600, self._deliveries_job, "Follow tracking links of active deliveries"),
            Job("vault", 60, self.writer.flush, "Write queued notes to Obsidian"),
            Job("saves", 60, self.notify_saves, "Tell you what was saved to the vault"),
            Job("logs", 3600, self.prune_logs, f"Keep {settings.log_retention_days} days of diagnostic logs"),
            Job("digest", 300, self.notifier.release_held, "Send notifications held in quiet hours"),
        ):
            self.jobs[job.name] = job
        self._tasks: list[asyncio.Task] = []
        self._google_alerted = False

    def _google(self, func: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[object]]:
        async def wrapped() -> object:
            if not self.oauth.configured or not self.oauth.connected:
                return "skipped: Google not connected"
            try:
                result = await func()
                self._google_alerted = False
                return result
            except GoogleAuthRequired as error:
                if not self._google_alerted:
                    self._google_alerted = True
                    await self.notifier.notify("Jarvis needs Google re-authorisation", str(error), 4,
                                               self.settings.public_url + "/#status", dedupe=None, tags="warning")
                raise
        return wrapped

    async def scan_events(self) -> dict:
        result = await self.events.scan_queue()
        await self.events.flush_notifications()
        added = await self.events.auto_add()
        if added:
            result["auto_added"] = added
        return result

    # vault save reports ----------------------------------------------------------
    async def prune_logs(self) -> dict:
        return self.diag.prune()

    async def post_brief(self, text: str) -> None:
        self.db.execute("INSERT INTO chat_messages (ts, role, content, trace) VALUES (?, 'activity', ?, ?)",
                        (time.time(), text, diag.current_trace_id()))

    async def announce_saves(self, saved: list[dict]) -> None:
        """Post what was just saved into the chat timeline (the ntfy summary is sent by the `saves` job)."""
        header = f"**Saved to your vault** ({len(saved)} note{'s' if len(saved) != 1 else ''})"
        self.db.execute("INSERT INTO chat_messages (ts, role, content, trace) VALUES (?, 'activity', ?, ?)",
                        (time.time(), header + "\n" + format_saves(saved), diag.current_trace_id()))

    async def notify_saves(self) -> str:
        if self.settings.vault_save_notify != "summary" or not self.notifier.enabled("saves"):
            self.db.execute("UPDATE vault_saves SET notified = 1 WHERE notified = 0")  # shown in chat; nothing to send
            return "off"
        rows = self.db.all("SELECT * FROM vault_saves WHERE notified = 0 ORDER BY id")
        if not rows:
            return "nothing new"
        last = float(self.db.get("saves.last_notified", 0))
        if time.time() - last < self.settings.vault_save_notify_minutes * 60:
            return f"{len(rows)} waiting"
        items = [{"path": r["path"], "title": r["path"].rsplit("/", 1)[-1].removesuffix(".md"),
                  "summary": r["summary"], "created": bool(r["created"])} for r in rows]
        lines = [f"- {'New' if i['created'] else 'Updated'}: {i['summary']}" for i in items[:15]]
        if len(items) > 15:
            lines.append(f"- …and {len(items) - 15} more")
        await self.notifier.notify(f"Jarvis saved {len(items)} note(s) to your vault", "\n".join(lines), priority=2,
                                   url=self.settings.public_url.rstrip("/") + "/#changes", tags="floppy_disk",
                                   category="saves")
        ids = [r["id"] for r in rows]
        self.db.execute(f"UPDATE vault_saves SET notified = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        self.db.set("saves.last_notified", time.time())
        return f"notified {len(items)}"

    # scheduler -------------------------------------------------------------
    async def _deliveries_job(self) -> dict:
        """Hourly tracking checks; the first time, also look back over the last 30 days of email."""
        result = {}
        if not self.db.get("deliveries.rechecked.v1") and self.oauth.configured and self.oauth.connected:
            try:  # parcels wrongly marked delivered by an email's progress graphic (before v0.9.3)
                result["corrected"] = await self.deliveries.recheck_delivered()
                self.db.set("deliveries.rechecked.v1", time.time())
            except GoogleError as error:
                result["corrected"] = f"failed: {error}"
        if not self.db.get("deliveries.looked_back") and self.oauth.configured and self.oauth.connected:
            try:
                result["look_back"] = await self.deliveries.look_back()
            except GoogleError as error:
                result["look_back"] = f"failed: {error}"
        return {**result, **await self.deliveries.run()}

    def _apply_calendar_choice(self) -> None:
        """Calendars ticked on the Status page replace GOOGLE_CALENDAR_IDS (read and offered for adding)."""
        chosen = self.db.get("calendars.enabled")
        if isinstance(chosen, list) and chosen:
            self.settings.google_calendar_ids = [str(c) for c in chosen]

    async def calendar_choices(self) -> list[dict]:
        enabled = set(self.settings.google_calendar_ids)
        items = await self.calendar.calendars()
        for item in items:
            item["enabled"] = item["id"] in enabled or (item["primary"] and "primary" in enabled)
        items.sort(key=lambda c: (not c["primary"], c["hidden"], c["name"].casefold()))
        return items

    def set_calendars(self, ids: list[str]) -> list[str]:
        ids = [str(i) for i in ids if str(i).strip()][:50] or ["primary"]
        self.db.set("calendars.enabled", ids)
        self.settings.google_calendar_ids = ids
        self.trigger("calendar")  # read the newly chosen calendars now
        return ids

    def _learn_home_names_from_captures(self) -> None:
        """Names remembered before Jarvis understood them ("the gas water heater is the entity …")."""
        from .ha import parse_alias
        learned = 0
        for row in self.db.all("SELECT text, ts FROM captures ORDER BY ts"):
            parsed = parse_alias(re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", row["text"]))
            if parsed:
                learned += self.db.execute(
                    "INSERT OR IGNORE INTO ha_aliases (alias, entity_id, source, updated) VALUES (?, ?, 'chat', ?)",
                    (parsed[0], parsed[1], row["ts"])).rowcount
        if learned:
            self.db.queue_note("home_names", "all")
            log.info("learned %d Home Assistant name(s) from earlier captures", learned)

    async def run_job(self, job: Job, manual: bool = False) -> None:
        if job.running:
            return
        job.running = True
        with diag.trace("job", job.name, always=manual) as trace:
            try:
                result = await job.func()
                job.last_ok = True
                job.last_result = str(result)[:300]
                if manual or not quiet_result(result):
                    trace["result"] = result
            except VaultUnavailable as error:
                job.last_ok = False
                job.last_result = f"vault offline: {error}"
                diag.warning("job", f"{job.name}: Obsidian is offline — work stays queued", error=str(error))
            except Exception as error:  # a failing job must not stop the others
                diag.error("job", f"{job.name} failed: {type(error).__name__}: {error}", error)
                job.last_ok = False
                job.last_result = f"{type(error).__name__}: {error}"[:300]
            finally:
                job.last_run = time.time()
                job.running = False
                if trace.get("persisted") or manual:
                    job.last_trace = trace["id"]

    async def _loop(self, job: Job, initial_delay: float) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            await self.run_job(job, manual=job.trigger.is_set())
            job.trigger.clear()
            try:
                await asyncio.wait_for(job.trigger.wait(), timeout=job.interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        for index, job in enumerate(self.jobs.values()):
            self._tasks.append(asyncio.create_task(self._loop(job, 3 + index * 2), name=f"job:{job.name}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def trigger(self, name: str) -> bool:
        job = self.jobs.get(name)
        if job is None:
            return False
        job.trigger.set()
        return True

    def history(self, limit: int = 40) -> list[dict]:
        """Everything Jarvis did or you decided, newest first, in one list: reminders, home actions (including each
        repeating one's latest run) and calendar suggestions added or dismissed."""
        tz = self.settings.tz
        items: list[dict] = []
        for r in self.db.all(
                "SELECT * FROM scheduled WHERE status IN ('done', 'failed', 'cancelled', 'dismissed') "
                "OR (status = 'scheduled' AND last_run IS NOT NULL) "
                "ORDER BY COALESCE(decided, last_run, created) DESC LIMIT ?", (limit,)):
            ts = r["decided"] if r["status"] in ("cancelled", "dismissed") and r["decided"] else (r["last_run"] or r["created"])
            status = {"scheduled": "done", "dismissed": "declined"}.get(r["status"], r["status"])
            detail = r["result"] if r["result"] and r["result"].casefold() not in ("done", "ok", "sent") \
                and r["status"] in ("done", "failed", "scheduled") else ""
            if r["status"] == "scheduled":
                detail = (detail + " · " if detail else "") + f"repeats {r['repeat']}"
            items.append({"ts": ts, "icon": "🏠" if r["kind"] == "ha" else "⏰", "text": r["text"], "status": status,
                          "detail": detail, "kind": "home" if r["kind"] == "ha" else "reminder"})
        for r in self.db.all(
                "SELECT * FROM event_proposals WHERE status IN ('added', 'dismissed', 'failed') AND decided IS NOT NULL "
                "AND error NOT LIKE 'auto:%' ORDER BY decided DESC LIMIT ?", (limit,)):
            status = {"added": "details added" if r["kind"] == "note" else "added to calendar",
                      "dismissed": "dismissed"}.get(r["status"], r["status"])
            when = self.events.present(r)["when"]
            items.append({"ts": r["decided"], "icon": "📝" if r["kind"] == "note" else "📅",
                          "text": r["title"] + (f" — {r['notes']}" if r["kind"] == "note" else ""), "status": status,
                          "detail": when + (f" · {r['error']}" if r["status"] == "failed" and r["error"] else ""),
                          "kind": "calendar", "url": self.events.present(r)["gmail_url"]})
        items.sort(key=lambda i: i["ts"] or 0, reverse=True)
        for item in items:
            item["at"] = datetime.fromtimestamp(item["ts"], tz).strftime("%a %d %b, %H:%M") if item["ts"] else ""
        return items[:limit]

    async def status(self) -> dict:
        llm, vault, home, web = await asyncio.gather(self.llm.health(), self.vault.health(), self.ha.health(),
                                                     self.web.health())
        return {
            "components": {
                "model": llm,
                "obsidian": vault,
                "google": self.oauth.status(),
                "ntfy": self.notifier.status(),
                "voice": self.speech.status(),
                "home": home,
                "web": web,
            },
            "vault_outbox": self.writer.pending(),
            "jobs": [
                {"name": j.name, "description": j.description, "interval": j.interval, "last_run": j.last_run,
                 "ok": j.last_ok, "result": j.last_result, "running": j.running, "trace": j.last_trace}
                for j in self.jobs.values()
            ],
            "counts": {
                "emails": self.db.one("SELECT COUNT(*) n FROM emails")["n"],
                "threads": self.db.one("SELECT COUNT(*) n FROM threads")["n"],
                "events": self.db.one("SELECT COUNT(*) n FROM events")["n"],
                "people": self.db.one("SELECT COUNT(DISTINCT path) n FROM people")["n"],
                "event_proposals": self.db.one("SELECT COUNT(*) n FROM event_proposals WHERE status = 'pending'")["n"],
                "event_scan_queue": self.db.one("SELECT COUNT(*) n FROM event_scan WHERE status = 'pending'")["n"],
            },
        }
