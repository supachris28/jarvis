"""Vault writer with an outbox.

Pipelines never write to Obsidian directly. They store structured data in SQLite and
queue a (kind, key) item. `VaultWriter.flush()` renders each queued note from the
database and writes it through the Obsidian REST API. If the PC is off, items stay
queued and are written the next time Obsidian is reachable. Every change is recorded
in `note_history` so it can be reverted from the web UI.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from .. import diag
from ..db import Database
from ..google.gmail import thread_url
from .client import ObsidianVault, VaultError, VaultUnavailable
from .markdown import join_frontmatter, link, merge_frontmatter, one_line, replace_block, safe_name, split_frontmatter

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
HISTORY_KEEP = 3000


@dataclass
class Block:
    name: str
    content: str
    heading: str | None = None


@dataclass
class NoteSpec:
    path: str
    title: str
    owned: dict = field(default_factory=dict)      # frontmatter Jarvis always sets
    defaults: dict = field(default_factory=dict)   # frontmatter set only if missing (lists unioned)
    blocks: list[Block] = field(default_factory=list)
    intro: str = ""                                # text placed under the title on creation
    summary: str = ""                              # one line telling Chris what was saved


Renderer = Callable[[str], Awaitable[NoteSpec | None]]
SavedCallback = Callable[[list[dict]], Awaitable[None]]


def format_saves(saved: list[dict], limit: int = 25) -> str:
    """Markdown list telling Chris what was saved."""
    lines = []
    for item in saved[:limit]:
        verb = "New" if item["created"] else "Updated"
        lines.append(f"- {verb}: [[{item['path'].removesuffix('.md')}|{item['title']}]] — {item['summary']}")
    if len(saved) > limit:
        lines.append(f"- …and {len(saved) - limit} more (see the Vault tab)")
    return "\n".join(lines)


HOME_NAMES_PATH = "Jarvis/Home names.md"


class VaultWriter:
    def __init__(self, db: Database, vault: ObsidianVault, tz) -> None:
        self.db = db
        self.vault = vault
        self.tz = tz
        self.renderers: dict[str, Renderer] = {
            "thread": self.render_thread,
            "event": self.render_event,
            "person": self.render_person,
            "journal": self.render_journal,
            "inbox": self.render_inbox,
            "contact": self.render_contact,
            "reminders": self.render_reminders,
            "home_names": self.render_home_names,
        }
        self.on_saved: SavedCallback | None = None

    # writing ---------------------------------------------------------------
    async def apply(self, spec: NoteSpec, actor: str) -> dict | None:
        """Write the note if it changed. Returns a saved-item record, or None if nothing changed."""
        existing = await self.vault.get_text(spec.path)
        if existing is not None:
            existing = existing.replace("\r\n", "\n")  # a CRLF note would otherwise never compare "unchanged"
        frontmatter, body = split_frontmatter(existing or "")
        if existing is None:
            body = f"# {spec.title}\n" + (f"\n{spec.intro.strip()}\n" if spec.intro.strip() else "")
        frontmatter = merge_frontmatter(frontmatter, spec.defaults)
        frontmatter.update(spec.owned)
        for block in spec.blocks:
            body = replace_block(body, block.name, block.content, block.heading)
        updated = join_frontmatter(frontmatter, body)
        if existing is not None and updated.strip() == existing.strip():
            diag.debug("vault", f"unchanged: {spec.path}")
            return None
        await self.vault.put_text(spec.path, updated)
        self.record(spec.path, actor, existing, updated)
        diag.event("vault", f"{'created' if existing is None else 'updated'} {spec.path}", summary=spec.summary,
                   actor=actor, chars=len(updated))
        item = {"path": spec.path, "title": spec.title, "summary": spec.summary or spec.title,
                "created": existing is None, "actor": actor}
        self.db.execute("INSERT INTO vault_saves (ts, path, summary, created, actor, notified) VALUES (?, ?, ?, ?, ?, ?)",
                        (time.time(), spec.path, item["summary"], int(item["created"]), actor,
                         0 if actor.startswith("pipeline") else 1))
        return item

    def record(self, path: str, actor: str, before: str | None, after: str) -> None:
        self.db.execute(
            "INSERT INTO note_history (ts, path, actor, before, after) VALUES (?, ?, ?, ?, ?)",
            (time.time(), path, actor, before, after),
        )
        self.db.execute(
            "DELETE FROM note_history WHERE id <= (SELECT MAX(id) FROM note_history) - ?", (HISTORY_KEEP,)
        )

    async def revert(self, history_id: int) -> str:
        row = self.db.one("SELECT * FROM note_history WHERE id = ?", (history_id,))
        if row is None:
            raise VaultError("Unknown change.")
        current = await self.vault.get_text(row["path"])
        restored = row["before"] if row["before"] is not None else ""
        if row["before"] is None:
            restored = f"<!-- Jarvis reverted its creation of this note on {datetime.now(self.tz):%Y-%m-%d %H:%M}. "\
                       "Delete it in Obsidian if not needed. -->\n"
        await self.vault.put_text(row["path"], restored)
        self.record(row["path"], "revert", current, restored)
        return row["path"]

    async def flush(self, limit: int = 200, actor: str = "pipeline", announce: bool = True) -> dict:
        rows = self.db.all(
            "SELECT kind, key FROM outbox WHERE attempts < ? ORDER BY queued LIMIT ?", (MAX_ATTEMPTS, limit)
        )
        saved: list[dict] = []
        try:
            return await self._flush(rows, actor, saved)
        finally:
            if saved and announce and self.on_saved is not None:
                await self.on_saved(saved)

    async def _flush(self, rows, actor: str, saved: list[dict]) -> dict:
        written = skipped = failed = 0
        for row in rows:
            kind, key = row["kind"], row["key"]
            try:
                renderer = self.renderers.get(kind)
                spec = await renderer(key) if renderer else None
                item = await self.apply(spec, f"{actor}:{kind}") if spec is not None else None
                if item:
                    written += 1
                    saved.append(item)
                else:
                    skipped += 1
                self.db.execute("DELETE FROM outbox WHERE kind = ? AND key = ?", (kind, key))
            except VaultUnavailable as error:
                diag.debug("vault", f"Obsidian unreachable — {self.pending()} note(s) stay queued", error=str(error))
                return {"written": written, "skipped": skipped, "failed": failed,
                        "pending": self.pending(), "offline": True, "saved": saved}
            except Exception as error:  # keep the rest of the outbox moving
                failed += 1
                log.warning("vault write %s/%s failed: %s", kind, key, error)
                self.db.execute(
                    "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE kind = ? AND key = ?",
                    (str(error)[:500], kind, key),
                )
        return {"written": written, "skipped": skipped, "failed": failed, "pending": self.pending(), "offline": False,
                "saved": saved}

    def pending(self) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM outbox WHERE attempts < ?", (MAX_ATTEMPTS,))
        return int(row["n"]) if row else 0

    # people ----------------------------------------------------------------
    def person_path(self, email: str) -> str | None:
        row = self.db.one("SELECT path FROM people WHERE email = ?", (email.casefold(),))
        return row["path"] if row else None

    def ensure_person(self, email: str, name: str, create: bool) -> str | None:
        email = email.strip().casefold()
        if not email:
            return None
        path = self.person_path(email)
        if path:
            self.db.queue_note("person", email)
            return path
        if not create:
            return None
        display = name.strip() if name and name.strip() and "@" not in name else ""
        if not display:
            return None
        path = f"People/{safe_name(display)}.md"
        self.db.execute("INSERT OR IGNORE INTO people (email, name, path) VALUES (?, ?, ?)", (email, display, path))
        self.db.queue_note("person", email)
        return path

    async def refresh_people(self) -> int:
        """Learn which existing People notes own which email addresses."""
        indexed = getattr(self.vault, "frontmatter_under", None)
        if indexed is not None:  # files backend: one query on the index instead of reading every note
            found = 0
            for full, frontmatter in await indexed("People/"):
                emails = frontmatter.get("emails") or frontmatter.get("email") or []
                for email in [emails] if isinstance(emails, str) else emails:
                    if isinstance(email, str) and "@" in email:
                        self.db.execute(
                            "INSERT INTO people (email, name, path) VALUES (?, ?, ?) "
                            "ON CONFLICT(email) DO UPDATE SET path = excluded.path, name = excluded.name",
                            (email.strip().casefold(), full.rsplit("/", 1)[-1].removesuffix(".md"), full))
                        found += 1
            return found
        found = 0
        folders = ["People/"]
        seen_folders: set[str] = set()
        while folders:
            folder = folders.pop()
            if folder in seen_folders or len(seen_folders) > 50:
                continue
            seen_folders.add(folder)
            for item in await self.vault.list_dir(folder):
                full = folder + item if not item.startswith(folder) else item
                if item.endswith("/"):
                    folders.append(full)
                    continue
                if not full.endswith(".md"):
                    continue
                note = await self.vault.get_note(full)
                if not note:
                    continue
                frontmatter = note.get("frontmatter") or {}
                emails = frontmatter.get("emails") or frontmatter.get("email") or []
                if isinstance(emails, str):
                    emails = [emails]
                title = full.rsplit("/", 1)[-1].removesuffix(".md")
                for email in emails:
                    if isinstance(email, str) and "@" in email:
                        self.db.execute(
                            "INSERT INTO people (email, name, path) VALUES (?, ?, ?) "
                            "ON CONFLICT(email) DO UPDATE SET path = excluded.path, name = excluded.name",
                            (email.strip().casefold(), title, full),
                        )
                        found += 1
        return found

    def known_people(self) -> list[tuple[str, str]]:
        """(name, path) for everyone Jarvis knows, from email and Contacts; longest names first."""
        rows = self.db.all("SELECT name, path FROM people UNION SELECT name, path FROM contacts")
        seen: dict[str, str] = {}
        for row in rows:
            if row["name"] and "@" not in row["name"]:
                seen.setdefault(row["name"], row["path"])
        return sorted(seen.items(), key=lambda item: -len(item[0]))

    # renderers -------------------------------------------------------------
    def _fmt_ts(self, ts: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime(fmt)

    def _person_link(self, email: str, name: str) -> str:
        path = self.person_path(email)
        return link(path, name or None) if path else (f"{name} <{email}>" if name else email)

    async def render_thread(self, thread_id: str) -> NoteSpec | None:
        thread = self.db.one("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        messages = self.db.all("SELECT * FROM emails WHERE thread_id = ? AND bulk = 0 ORDER BY ts", (thread_id,))
        if thread is None or not messages:
            return None
        participants: dict[str, str] = {}
        for message in messages:
            participants.setdefault(message["from_addr"], message["from_name"])
            for addr in json.loads(message["to_addrs"]):
                participants.setdefault(addr[0], addr[1])
        people_links = [link(p) for p in {self.person_path(a) for a in participants} if p]
        sections = []
        for message in messages:
            attachments = json.loads(message["attachments"])
            header = f"### {self._fmt_ts(message['ts'])} · {self._person_link(message['from_addr'], message['from_name'])}"
            lines = [header]
            if attachments:
                lines.append("Attachments: " + ", ".join(attachments))
            lines.append("")
            lines.append(message["body"].strip() or message["snippet"])
            sections.append("\n".join(lines))
        labels = sorted({label for m in messages for label in json.loads(m["labels"])})
        return NoteSpec(
            path=thread["path"],
            title=thread["subject"] or "(no subject)",
            owned={
                "type": "email-thread",
                "source": "gmail",
                "thread_id": thread_id,
                "subject": thread["subject"],
                "people": people_links,
                "participants": sorted(participants),
                "first": self._fmt_ts(messages[0]["ts"]),
                "last": self._fmt_ts(messages[-1]["ts"]),
                "labels": labels,
                "gmail": thread_url(thread_id, self.db.get("gmail.me", "")),
            },
            blocks=[Block("messages", "\n\n".join(sections))],
            summary=(f"Email “{one_line(thread['subject'], 70) or '(no subject)'}” — {len(messages)} message(s), "
                     f"latest from {messages[-1]['from_name'] or messages[-1]['from_addr']}"),
        )

    async def render_event(self, event_id: str) -> NoteSpec | None:
        event = self.db.one("SELECT * FROM events WHERE event_id = ?", (event_id,))
        if event is None:
            return None
        attendees = json.loads(event["attendees"])
        people = [link(p) for p in {self.person_path(a["email"]) for a in attendees if a.get("email")} if p]
        who = [self._person_link(a.get("email", ""), a.get("name", "")) for a in attendees if a.get("email")]
        details = [f"- When: {event['start']} → {event['end']}"]
        if event["location"]:
            details.append(f"- Where: {event['location']}")
        if who:
            details.append("- With: " + ", ".join(who))
        if event["status"] == "cancelled":
            details.append("- Status: **cancelled**")
        if event["description"].strip():
            details.append("\n" + event["description"].strip()[:4000])
        day = event["start"][:10]
        return NoteSpec(
            path=event["path"],
            title=event["summary"] or "(untitled event)",
            owned={
                "type": "event",
                "source": "google-calendar",
                "event_id": event_id,
                "start": event["start"],
                "end": event["end"],
                "location": event["location"],
                "people": people,
                "status": event["status"],
                "day": link(f"Journal/{day[:4]}/{day}.md", day),
                "calendar": event["html_link"],
            },
            blocks=[Block("details", "\n".join(details))],
            summary=(f"Event “{one_line(event['summary'], 70) or 'untitled'}” on {day}"
                     + (f" at {event['location']}" if event["location"] else "")
                     + (" (cancelled)" if event["status"] == "cancelled" else "")),
        )

    async def render_person(self, email: str) -> NoteSpec | None:
        person = self.db.one("SELECT * FROM people WHERE email = ?", (email,))
        if person is None:
            return None
        addresses = [r["email"] for r in self.db.all("SELECT email FROM people WHERE path = ?", (person["path"],))]
        marks = ",".join("?" * len(addresses))
        emails = self.db.all(
            f"SELECT e.thread_id, MAX(e.ts) AS ts, t.path, t.subject FROM emails e JOIN threads t USING (thread_id) "
            f"WHERE e.bulk = 0 AND (e.from_addr IN ({marks}) OR "
            f"EXISTS (SELECT 1 FROM json_each(e.to_addrs) j WHERE json_extract(j.value, '$[0]') IN ({marks}))) "
            f"GROUP BY e.thread_id ORDER BY ts DESC LIMIT 15",
            addresses + addresses,
        )
        events = self.db.all(
            f"SELECT * FROM events WHERE status != 'cancelled' AND EXISTS (SELECT 1 FROM json_each(attendees) j "
            f"WHERE json_extract(j.value, '$.email') IN ({marks})) ORDER BY start DESC LIMIT 15",
            addresses,
        )
        lines = []
        if emails:
            lines.append("**Recent email**")
            lines += [f"- {self._fmt_ts(r['ts'], '%Y-%m-%d')} · {link(r['path'], one_line(r['subject'], 90) or 'email')}"
                      for r in emails]
        if events:
            lines.append("")
            lines.append("**Events together**")
            lines += [f"- {r['start'][:10]} · {link(r['path'], one_line(r['summary'], 90) or 'event')}" for r in events]
        if not emails and not events:
            lines.append("No email or events recorded yet.")
        return NoteSpec(
            path=person["path"],
            title=person["name"],
            defaults={"type": "person", "emails": addresses, "aliases": [person["name"]], "tags": ["person"]},
            owned={"updated": f"{datetime.now(self.tz):%Y-%m-%d}"},
            blocks=[Block("timeline", "\n".join(lines), heading="## Timeline")],
            summary=(f"{person['name']} ({', '.join(addresses[:2])}): timeline — {len(emails)} email thread(s), "
                     f"{len(events)} event(s)" + (f"; latest “{one_line(emails[0]['subject'], 50)}”" if emails else "")),
        )

    async def render_contact(self, resource_name: str) -> NoteSpec | None:
        row = self.db.one("SELECT * FROM contacts WHERE resource_name = ?", (resource_name,))
        if row is None:
            return None
        c = json.loads(row["data"])
        known = dict(self.known_people())
        lines = []
        if c["phones"]:
            lines.append("- Phone: " + ", ".join(c["phones"]))
        if c["emails"]:
            lines.append("- Email: " + ", ".join(c["emails"]))
        if c["birthday"]:
            lines.append(f"- Birthday: {c['birthday'].lstrip('-')}")
        if c["organization"]:
            lines.append(f"- Work: {c['organization']}")
        for address in c["addresses"]:
            lines.append(f"- Address: {address}")
        for relation in c["relations"]:
            who = relation["person"]
            target = known.get(who)
            shown = link(target, who) if target else who
            lines.append(f"- {relation['type'].capitalize() or 'Related'}: {shown}")
        if c["notes"]:
            lines.append("\n" + c["notes"][:2000])
        defaults = {"type": "person", "aliases": [c["name"]] + c["nicknames"], "tags": ["person", "contact"]}
        if c["emails"]:
            defaults["emails"] = c["emails"]
        if c["phones"]:
            defaults["phones"] = c["phones"]
        if c["birthday"]:
            defaults["birthday"] = c["birthday"]
        if c["relations"]:
            defaults["relations"] = [f"{r['type']}: {r['person']}" for r in c["relations"]]
        bits = []
        if c["phones"]:
            bits.append(f"{len(c['phones'])} phone number(s)")
        if c["birthday"]:
            bits.append(f"birthday {c['birthday'].lstrip('-')}")
        if c["relations"]:
            bits.append(", ".join(f"{r['type']} {r['person']}" for r in c["relations"][:2]))
        return NoteSpec(
            path=row["path"],
            title=c["name"],
            defaults=defaults,
            owned={"google_contact": resource_name},
            blocks=[Block("contact", "\n".join(lines) or "- (no details)", heading="## Contact")],
            summary=f"Contact {c['name']}" + (f": {'; '.join(bits)}" if bits else ""),
        )

    async def render_reminders(self, _key: str = "all") -> NoteSpec:
        rows = self.db.all("SELECT * FROM scheduled WHERE status IN ('scheduled', 'proposed') ORDER BY due IS NULL, due")
        done = self.db.all("SELECT * FROM scheduled WHERE status IN ('done', 'failed') AND last_run > ? "
                           "ORDER BY last_run DESC LIMIT 20", (time.time() - 7 * 86400,))
        repeat_icon = {"daily": "every day", "weekdays": "every weekday", "weekly": "every week",
                       "monthly": "every month"}

        def when(ts: float | None) -> str:
            return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d %H:%M") if ts else "now"

        lines = []
        for r in rows:
            kind = "🏠 " if r["kind"] == "ha" else ""
            wait = " (awaiting your OK in Jarvis)" if r["status"] == "proposed" else ""
            rep = f" 🔁 {repeat_icon[r['repeat']]}" if r["repeat"] in repeat_icon else ""
            day = datetime.fromtimestamp(r["due"], self.tz).strftime("%Y-%m-%d") if r["due"] else ""
            lines.append(f"- [ ] {kind}{r['text']} ⏳ {day} {when(r['due'])[11:]}{rep}{wait} ^r{r['id']}".replace("  ", " "))
        if not lines:
            lines.append("- Nothing scheduled.")
        lines += ["", "**Done in the last week**"] if done else []
        for r in done:
            mark = "x" if r["status"] == "done" else "-"
            lines.append(f"- [{mark}] {r['text']} ✅ {when(r['last_run'])[:10]}{' (failed: ' + r['result'] + ')' if r['status'] == 'failed' else ''}")
        return NoteSpec(
            path="Jarvis/Reminders.md",
            title="Reminders",
            defaults={"type": "reminders", "tags": ["jarvis", "tasks"]},
            blocks=[Block("reminders", "\n".join(lines))],
            intro="Reminders and scheduled home actions. Tick an open item here to cancel it; Jarvis picks that up.",
            summary=f"Reminders: {len(rows)} open" + (f", next — {rows[0]['text']} ({when(rows[0]['due'])})" if rows else ""),
        )

    async def render_home_names(self, _key: str = "all") -> NoteSpec:
        rows = self.db.all("SELECT alias, entity_id FROM ha_aliases WHERE source = 'chat' ORDER BY alias")
        lines = [f"- {r['alias']} → `{r['entity_id']}`" for r in rows] or ["- (none yet)"]
        return NoteSpec(
            path=HOME_NAMES_PATH,
            title="Home names",
            defaults={"type": "home-names", "tags": ["jarvis", "home"]},
            blocks=[Block("home-names", "\n".join(lines))],
            intro=("Names Jarvis uses for Home Assistant entities. Teach one in chat with “remember the gas water "
                   "heater is water_heater.thermostat1”. You can also add your own lines below the managed block, "
                   "in the same form: `- name → domain.entity_id`."),
            summary=f"Home names: {len(rows)} name(s)" + (f", latest — {rows[-1]['alias']}" if rows else ""),
        )

    async def render_journal(self, day: str) -> NoteSpec:
        entries = self.db.all("SELECT * FROM journal WHERE day = ? ORDER BY ts", (day,))
        lines = [f"- {self._fmt_ts(e['ts'], '%H:%M')} {e['text']}" for e in entries] or ["- Nothing recorded."]
        blocks = []
        brief = self.db.get(f"brief.{day}")
        if brief:
            blocks.append(Block("brief", brief, heading="## Morning brief"))
        blocks.append(Block("log", "\n".join(lines), heading="## Jarvis log"))
        return NoteSpec(
            path=f"Journal/{day[:4]}/{day}.md",
            title=day,
            defaults={"type": "journal", "date": day, "tags": ["journal"]},
            blocks=blocks,
            summary=f"Journal {day}: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
                    + (f"; latest — {one_line(entries[-1]['text'], 90)}" if entries else ""),
        )

    async def render_inbox(self, day: str) -> NoteSpec:
        captures = self.db.all("SELECT * FROM captures WHERE day = ? ORDER BY ts", (day,))
        lines = [f"- {self._fmt_ts(c['ts'], '%H:%M')} {c['text']}" for c in captures] or ["- (empty)"]
        return NoteSpec(
            path=f"Inbox/{day} Captures.md",
            title=f"Captures {day}",
            defaults={"type": "captures", "date": day, "tags": ["inbox"],
                      "day": link(f"Journal/{day[:4]}/{day}.md", day)},
            blocks=[Block("captures", "\n".join(lines))],
            intro="Things you asked Jarvis to remember. Move or refile them freely.",
            summary=f"Captured: “{one_line(captures[-1]['text'], 120)}”" if captures else f"Captures {day}",
        )
