"""People and families: who's who, how they're linked, and what Jarvis should know before you see them.

Everything lives in the People notes' properties, so Obsidian shows it too:
  relation: friend            family: Topliss              birthday: 1986-03-12
  partner: "[[People/Emily Topliss|Emily Topliss]]"
  children: ["[[People/Sam Topliss|Sam Topliss]]"]       parents: [...]
Links are kept both ways (Ben's partner Emily ↔ Emily's partner Ben; a child ↔ their parents), and family members
without an email (children, say) get a small note of their own.

Jarvis notices who you're in touch with regularly (email both ways, calendar events) and asks about anyone it knows
little about — how you know them, their birthday, their family — on the People tab and in the weekly review.
Before a calendar event with people you know it sends a short "before you meet" card.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

from .. import diag
from ..assistant.facts import parse_date
from ..vault.client import VaultError
from ..vault.markdown import join_frontmatter, link, one_line, safe_name, split_frontmatter

LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
FIELDS = ("relation", "family", "birthday", "partner", "children", "parents", "phone")
FREQUENT = 3          # emails/events in the last 90 days that make someone "regular"


def linked_paths(value) -> list[str]:
    """Paths in a property: '[[People/Emily Topliss|Emily]]' or a list of them (also plain names)."""
    values = value if isinstance(value, list) else [value] if value else []
    paths = []
    for item in values:
        for match in LINK.finditer(str(item)):
            target = match.group(1).strip()
            paths.append(target if target.endswith(".md") else target + ".md")
    return paths


def display(value) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    out = []
    for item in values:
        text = LINK.sub(lambda m: m.group(2) or m.group(1).rsplit("/", 1)[-1], str(item)).strip()
        if text:
            out.append(text)
    return out


class People:
    def __init__(self, settings, db, vault, writer, assistant=None) -> None:
        self.settings = settings
        self.db = db
        self.vault = vault
        self.writer = writer
        self.assistant = assistant   # for the people index (names, aliases) and birthdays

    # ---------------------------------------------------------------- reading
    async def notes(self) -> dict[str, dict]:
        """{path: properties} for every People note."""
        found: dict[str, dict] = {}
        indexed = getattr(self.vault, "frontmatter_under", None)
        if indexed is not None:
            try:
                return {path: props or {} for path, props in await indexed("People/") if path.endswith(".md")}
            except VaultError:
                pass
        try:
            pending = ["People"]
            while pending:
                folder = pending.pop()
                for name in await self.vault.list_dir(folder):
                    path = f"{folder}/{name}".replace("//", "/")
                    if name.endswith("/"):
                        pending.append(path.rstrip("/"))
                    elif name.endswith(".md"):
                        note = await self.vault.get_note(path)
                        found[path] = (note or {}).get("frontmatter") or {}
        except VaultError as error:
            diag.debug("people", f"couldn't list People notes: {error}")
        return found

    def interactions(self, days: int = 90) -> dict[str, dict]:
        """{path: {count, last}} — emails with them (either way) and calendar events with them."""
        since = time.time() - days * 86400
        addresses: dict[str, str] = {r["email"]: r["path"] for r in self.db.all("SELECT email, path FROM people")}
        stats: dict[str, dict] = {}

        def bump(path: str, ts: float) -> None:
            item = stats.setdefault(path, {"count": 0, "last": 0.0})
            item["count"] += 1
            item["last"] = max(item["last"], ts)
        for row in self.db.all("SELECT from_addr, to_addrs, outgoing, ts, thread_id FROM emails "
                               "WHERE bulk = 0 AND ts > ?", (since,)):
            if row["outgoing"]:
                try:
                    to = [a for a, _ in json.loads(row["to_addrs"] or "[]")]
                except (ValueError, TypeError):
                    to = []
                for address in {a.casefold() for a in to}:
                    if address in addresses:
                        bump(addresses[address], row["ts"])
            elif row["from_addr"] in addresses:
                bump(addresses[row["from_addr"]], row["ts"])
        for row in self.db.all("SELECT attendees, start FROM events WHERE status != 'cancelled' AND start >= ?",
                               ((datetime.now(self.settings.tz).date() - timedelta(days=days)).isoformat(),)):
            try:
                attendees = json.loads(row["attendees"] or "[]")
            except ValueError:
                attendees = []
            for attendee in attendees:
                path = addresses.get((attendee.get("email") or "").casefold())
                if path and not attendee.get("self"):
                    try:
                        ts = datetime.fromisoformat(row["start"][:19]).timestamp()
                    except ValueError:
                        ts = time.time()
                    bump(path, min(ts, time.time()))
        return stats

    def _birthday(self, value) -> dict:
        parsed = parse_date(value) if value else None
        if not parsed:
            return {"birthday": str(value or ""), "birthday_text": "", "birthday_days": None}
        year, month, day = parsed
        today = datetime.now(self.settings.tz).date()
        nxt = None
        for y in (today.year, today.year + 1):
            try:
                candidate = date(y, month, day)
            except ValueError:
                candidate = date(y, 3, 1)
            if candidate >= today:
                nxt = candidate
                break
        days = (nxt - today).days if nxt else None
        text = f"{date(2000, month, day):%-d %B}" + (f" {year}" if year else "")
        if nxt and year:
            text += f" (turns {nxt.year - year}"
            text += ", " + ("today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days") + ")" \
                if days is not None and days <= 30 else ")"
        elif days is not None and days <= 30:
            text += " (" + ("today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days") + ")"
        return {"birthday": f"{year:04d}-{month:02d}-{day:02d}" if year else f"--{month:02d}-{day:02d}",
                "birthday_text": text, "birthday_days": days}

    async def directory(self) -> dict:
        """Everyone with a People note, their families and who Jarvis would like to know more about."""
        notes = await self.notes()
        stats = self.interactions()
        contacts = {r["path"]: r["birthday"] for r in self.db.all("SELECT path, birthday FROM contacts WHERE path != ''")}
        people = []
        for path, props in notes.items():
            name = path.rsplit("/", 1)[-1].removesuffix(".md")
            item = {"path": path, "name": name, "relation": ", ".join(display(props.get("relation"))),
                    "family": ", ".join(display(props.get("family"))),
                    "partner": display(props.get("partner")), "children": display(props.get("children")),
                    "parents": display(props.get("parents")), "phone": ", ".join(display(props.get("phone") or
                                                                                          props.get("phones")))}
            birthday = props.get("birthday") or props.get("birthdate") or props.get("dob") or contacts.get(path, "")
            item |= self._birthday(birthday)
            seen = stats.get(path, {"count": 0, "last": 0})
            item["contact_count"] = seen["count"]
            item["last_contact"] = seen["last"]
            item["last_contact_text"] = self._ago(seen["last"]) if seen["last"] else ""
            item["missing"] = [f for f, ok in (("how you know them", item["relation"]), ("birthday", item["birthday_text"]),
                                                ("family", item["family"] or item["partner"] or item["children"]))
                               if not ok]
            people.append(item)
        people.sort(key=lambda p: (-p["contact_count"], p["name"]))
        families: dict[str, list[dict]] = {}
        for p in people:
            for family in display(notes[p["path"]].get("family")):
                families.setdefault(family, []).append(p)
        return {"people": people, "families": [{"name": k, "members": v} for k, v in sorted(families.items())],
                "prompts": self.prompts(people=people)}

    @staticmethod
    def _ago(ts: float) -> str:
        days = int((time.time() - ts) // 86400)
        return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 60 \
            else f"{days // 30} months ago"

    def prompts(self, limit: int = 5, people: list[dict] | None = None) -> list[dict]:
        """People you're in touch with regularly that Jarvis knows little about (not ones you said to skip)."""
        if people is None:
            people = self.db.get("people.directory_cache") or []
        skipped = set(self.db.get("people.skip") or [])
        asks = []
        for p in people:
            if p["contact_count"] < FREQUENT or not p["missing"] or p["path"] in skipped:
                continue
            asks.append({"path": p["path"], "name": p["name"], "missing": p["missing"],
                         "why": f"{p['contact_count']} emails/events in 3 months — I don't know their "
                                + " or ".join(p["missing"])})
        if people:
            self.db.set("people.directory_cache", [{k: p[k] for k in ("path", "name", "contact_count", "missing")}
                                                   for p in people[:200]])
        return asks[:limit]

    def skip(self, path: str) -> None:
        skipped = list(self.db.get("people.skip") or [])
        if path not in skipped:
            skipped.append(path)
        self.db.set("people.skip", skipped[-500:])

    async def person(self, path: str) -> dict | None:
        note = await self.vault.get_note(path)
        if note is None:
            return None
        props = note.get("frontmatter") or {}
        directory = {p["path"]: p for p in (await self.directory())["people"]}
        item = directory.get(path) or {"path": path, "name": path.rsplit("/", 1)[-1].removesuffix(".md")}
        addresses = [r["email"] for r in self.db.all("SELECT email FROM people WHERE path = ?", (path,))]
        emails = []
        if addresses:
            marks = ",".join("?" * len(addresses))
            emails = [dict(r) for r in self.db.all(
                f"SELECT e.thread_id, MAX(e.ts) AS ts, e.subject FROM emails e WHERE e.bulk = 0 AND (e.from_addr IN "
                f"({marks}) OR EXISTS (SELECT 1 FROM json_each(e.to_addrs) j WHERE json_extract(j.value, '$[0]') IN "
                f"({marks}))) GROUP BY e.thread_id ORDER BY ts DESC LIMIT 5", addresses + addresses)]
        tz = self.settings.tz
        for e in emails:
            e["when"] = datetime.fromtimestamp(e["ts"], tz).strftime("%a %d %b")
        upcoming = []
        if addresses:
            marks = ",".join("?" * len(addresses))
            upcoming = [{"summary": r["summary"], "start": r["start"]} for r in self.db.all(
                f"SELECT summary, start FROM events WHERE status != 'cancelled' AND start >= ? AND EXISTS (SELECT 1 FROM "
                f"json_each(attendees) j WHERE json_extract(j.value, '$.email') IN ({marks})) ORDER BY start LIMIT 5",
                [datetime.now(tz).date().isoformat()] + addresses)]
        first = item["name"].split()[0].casefold()
        tasks = [t["text"] for t in self.db.all("SELECT text FROM tasks WHERE list = 'todo' AND done IS NULL")
                 if re.search(rf"\b{re.escape(first)}\b", t["text"], re.I)] if len(first) > 2 else []
        _, body = split_frontmatter(note.get("content") or "")
        added = [line[2:] for line in body.splitlines() if re.match(r"- \d{4}-\d\d-\d\d \d\d:\d\d — ", line)][-3:]
        return item | {"emails": [{**e, "email_url": f"/#email?thread={e['thread_id']}"} for e in emails],
                       "upcoming": upcoming, "tasks": tasks[:5], "recent_notes": added,
                       "properties": {k: props.get(k) for k in FIELDS if props.get(k)}}

    # ---------------------------------------------------------------- writing
    async def resolve(self, name: str, family: str = "", create: bool = True) -> str | None:
        """A person's note path from a name ('Emily', 'Emily Topliss'); creates a small note if they're new."""
        name = re.sub(r"\s+", " ", LINK.sub(lambda m: m.group(2) or m.group(1).rsplit("/", 1)[-1], name)).strip(" ,.")
        if not name:
            return None
        notes = await self.notes()
        by_name = {p.rsplit("/", 1)[-1].removesuffix(".md").casefold(): p for p in notes}
        if name.casefold() in by_name:
            return by_name[name.casefold()]
        if " " not in name and family:   # "Sam" in the Topliss family → Sam Topliss
            full = f"{name} {family}".casefold()
            if full in by_name:
                return by_name[full]
        matches = [p for n, p in by_name.items() if n.split()[0] == name.casefold()]
        if family and " " not in name:   # "Sam" in the Topliss family isn't Sam Jones
            matches = [p for p in matches if family.casefold() in [f.casefold() for f in display(notes[p].get("family"))]
                       or p.rsplit("/", 1)[-1].removesuffix(".md").casefold().endswith(" " + family.casefold())]
        if len(matches) == 1:
            return matches[0]
        if not create:
            return None
        full_name = f"{name} {family}" if " " not in name and family and family[:1].isupper() else name
        path = f"People/{safe_name(full_name, 70)}.md"
        props = {"type": "person", "tags": ["person"]}
        if family:
            props["family"] = family
        await self.vault.put_text(path, join_frontmatter(props, f"# {full_name}\n"))
        self.writer.record(path, "people", None, join_frontmatter(props, f"# {full_name}\n"))
        diag.event("people", f"created {path}")
        return path

    async def set_properties(self, path: str, updates: dict, actor: str = "you") -> None:
        text = await self.vault.get_text(path)
        if text is None:
            raise VaultError(f"There's no note at {path}.")
        props, body = split_frontmatter(text)
        props = dict(props or {})
        for key, value in updates.items():
            if value in (None, "", []):
                props.pop(key, None)
            elif key in ("children", "parents"):
                current = props.get(key) or []
                current = current if isinstance(current, list) else [current]
                for item in (value if isinstance(value, list) else [value]):
                    if linked_paths(item) and set(linked_paths(item)) & {q for c in current for q in linked_paths(c)}:
                        continue
                    current.append(item)
                props[key] = current
            else:
                props[key] = value
        updated = join_frontmatter(props, body)
        if updated.strip() != text.strip():
            await self.vault.put_text(path, updated)
            self.writer.record(path, actor, text, updated)

    async def update(self, path: str, fields: dict) -> dict:
        """Save what you told Jarvis about someone, keeping family links both ways."""
        name = path.rsplit("/", 1)[-1].removesuffix(".md")
        me = link(path, name)
        updates: dict = {}
        family = str(fields.get("family") or "").strip()[:60]
        own, _ = split_frontmatter(await self.vault.get_text(path) or "")
        household = family or ", ".join(display((own or {}).get("family")))   # for finding "Sam" in this family
        if "relation" in fields:
            updates["relation"] = str(fields["relation"] or "").strip()[:80]
        if family:
            updates["family"] = family
        if fields.get("birthday"):
            parsed = parse_date(str(fields["birthday"]))
            if parsed:
                year, month, day = parsed
                updates["birthday"] = f"{year:04d}-{month:02d}-{day:02d}" if year else f"--{month:02d}-{day:02d}"
        if fields.get("phone"):
            updates["phone"] = str(fields["phone"]).strip()[:40]
        linked: list[tuple[str, str]] = []   # (path, relationship from the other side)
        if fields.get("partner"):
            other = await self.resolve(str(fields["partner"]), household)
            if other and other != path:
                updates["partner"] = link(other, other.rsplit("/", 1)[-1].removesuffix(".md"))
                linked.append((other, "partner"))
        for key, back in (("children", "parents"), ("parents", "children")):
            names = fields.get(key)
            if not names:
                continue
            names = names if isinstance(names, list) else re.split(r"\s*(?:,|;|\band\b|&)\s*", str(names))
            paths = [p for p in [await self.resolve(n, household) for n in names if n.strip()] if p and p != path]
            if paths:
                updates[key] = [link(p, p.rsplit("/", 1)[-1].removesuffix(".md")) for p in paths]
                linked += [(p, back) for p in paths]
        await self.set_properties(path, updates)
        for other, relationship in linked:   # the other side of each link
            text = await self.vault.get_text(other) or ""
            props, _ = split_frontmatter(text)
            change: dict = {}
            if relationship == "partner":
                if not props.get("partner"):
                    change["partner"] = me
            else:
                change[relationship] = [me]
            if household and not props.get("family"):
                change["family"] = household
            if change:
                await self.set_properties(other, change, actor="people")
        diag.event("people", f"updated {path}", fields=sorted(updates))
        self.db.set("people.directory_cache", None)
        return await self.person(path) or {}

    async def create(self, name: str, fields: dict) -> dict:
        path = await self.resolve(name, str(fields.get("family") or ""), create=True)
        if not path:
            raise VaultError("A name is needed.")
        return await self.update(path, fields) if any(fields.values()) else (await self.person(path) or {})

    # ---------------------------------------------------------------- before you meet
    def known_attendees(self, event: dict, addresses: dict[str, str]) -> list[str]:
        try:
            attendees = json.loads(event["attendees"] or "[]") if isinstance(event["attendees"], str) else event["attendees"]
        except ValueError:
            attendees = []
        paths = [addresses[(a.get("email") or "").casefold()] for a in attendees
                 if not a.get("self") and (a.get("email") or "").casefold() in addresses]
        return list(dict.fromkeys(paths))

    async def briefing(self, path: str) -> str:
        """A few lines about someone, for just before you see them."""
        person = await self.person(path)
        if not person:
            return ""
        bits = [f"**{person['name']}**" + (f" — {person['relation']}" if person.get("relation") else "")
                + (f" ({person['family']} family)" if person.get("family") else "")]
        family = person.get("partner", []) + person.get("children", [])
        if family:
            bits.append("Family: " + ", ".join(family))
        if person.get("birthday_days") is not None and person["birthday_days"] <= 30:
            bits.append(f"🎂 Birthday {person['birthday_text']}")
        if person.get("emails"):
            last = person["emails"][0]
            bits.append(f"Last email: [{one_line(last['subject'], 60) or '(no subject)'}]({last['email_url']}) ({last['when']})")
        if person.get("tasks"):
            bits.append("Your to-dos: " + "; ".join(person["tasks"][:3]))
        if person.get("recent_notes"):
            bits.append("Your notes: " + " / ".join(one_line(n, 80) for n in person["recent_notes"][-2:]))
        return "\n".join(f"- {b}" if i else b for i, b in enumerate(bits))

    async def meeting_prep(self, notifier, lead_minutes: int = 120) -> int:
        """A 'before you meet' card for events starting within the next two hours that involve people you know."""
        tz = self.settings.tz
        now = datetime.now(tz)
        addresses = {r["email"]: r["path"] for r in self.db.all("SELECT email, path FROM people")}
        rows = self.db.all("SELECT * FROM events WHERE status != 'cancelled' AND all_day = 0 AND start >= ? AND start < ?",
                           (now.isoformat()[:16], (now + timedelta(minutes=lead_minutes)).isoformat()[:16]))
        index = await self.assistant.people_index() if self.assistant is not None else []
        sent = 0
        for row in rows:
            paths = self.known_attendees(dict(row), addresses)
            if self.assistant is not None:   # people named in the title ("Zoom with Ben and Emily")
                paths += [p for p, _ in self.assistant.match_people(row["summary"] or "", index) if p not in paths]
            if not paths:
                continue
            parts = [await self.briefing(p) for p in paths[:4]]
            parts = [p for p in parts if p]
            if not parts:
                continue
            start = datetime.fromisoformat(row["start"]).astimezone(tz)
            result = await notifier.notify(
                f"Before {one_line(row['summary'] or 'your event', 50)} ({start:%H:%M})", "\n\n".join(parts), 3,
                self.settings.public_url.rstrip("/") + f"/#people?p={quote(paths[0], safe='')}",
                dedupe=f"prep:{row['event_id']}:{row['start']}", tags="busts_in_silhouette", category="people")
            sent += int(result != "duplicate")
        return sent
