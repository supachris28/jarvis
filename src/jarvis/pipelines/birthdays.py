"""A heads-up a week before each birthday (and the day before), with gift ideas from the person's note.

Birthdays come from everywhere Jarvis already looks (Google Contacts, People notes, vault mentions). Ideas are lines in
the person's note that mention gifts, wish lists or what they like — or everything under a heading such as
"Gift ideas". Script only; no model.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from .. import diag
from ..config import Settings
from ..notify import Notifier
from ..vault.markdown import one_line, split_frontmatter

IDEA_HEADING = re.compile(r"^#{1,6}\s*.*\b(gift|present|ideas?|wish ?list|likes)\b", re.I)
IDEA_LINE = re.compile(r"\b(gift|present|wish ?list|would (?:like|love)|wants?\b|likes?\b|loves?\b|into\b|fan of|"
                       r"collects?|hobb(?:y|ies)|favou?rite|enjoys?)", re.I)
NOT_IDEA = re.compile(r"\b(allerg|doesn'?t like|does not like|hates|dislikes?|can'?t stand)\b", re.I)


def gift_ideas(note: str, limit: int = 4) -> list[str]:
    """Lines from a person's note that hint at a present."""
    _, body = split_frontmatter(note or "")
    ideas: list[str] = []
    under_heading = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            under_heading = bool(IDEA_HEADING.match(line))
            continue
        text = re.sub(r"^[-*+]\s+(\[.\]\s+)?", "", line).strip()
        text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]|\[\[([^\]]+)\]\]",
                      lambda m: m.group(2) or (m.group(3) or "").rsplit("/", 1)[-1], text)
        if len(text) < 3 or text.startswith(("<!--", "%%")):
            continue
        if under_heading:
            candidates = [text]
        else:  # in running text, only the sentence that mentions it
            candidates = [c for c in re.split(r"(?<=[.!?;])\s+", text) if IDEA_LINE.search(c)]
        for candidate in candidates:
            if not NOT_IDEA.search(candidate) and len(ideas) < limit:
                ideas.append(one_line(candidate, 110))
        if len(ideas) >= limit:
            break
    return ideas


class BirthdayReminders:
    def __init__(self, settings: Settings, notifier: Notifier, vault, source) -> None:
        self.settings = settings
        self.notifier = notifier
        self.vault = vault
        self.source = source  # async () -> list[Birthday]

    async def run(self) -> dict:
        now = datetime.now(self.settings.tz)
        if now.hour < 9:
            return {"sent": 0, "note": "after 09:00"}
        today = now.date()
        sent = 0
        for b in await self.source():
            when = b.next_date(today)
            days = (when - today).days
            if days not in (7, 1):
                continue
            turns = f" turns {when.year - b.year}" if b.year else "'s birthday is"
            title = f"🎂 {b.name}{turns} {'tomorrow' if days == 1 else f'on {when:%a} {when.day} {when:%b}'}"
            ideas: list[str] = []
            if b.path:
                try:
                    ideas = gift_ideas(await self.vault.get_text(b.path) or "")
                except Exception as error:  # noqa: BLE001 — the heads-up still goes out without ideas
                    diag.debug("birthdays", f"couldn't read {b.path}: {error}")
            if ideas:
                message = "Ideas from your notes:\n" + "\n".join(f"- {i}" for i in ideas)
            elif b.path:
                message = "No gift ideas in their note yet — tap to open it and add some."
            else:
                message = "In a week." if days == 7 else "Tomorrow."
            url = self.settings.public_url.rstrip("/") + (
                f"/#note?path={quote(b.path, safe='')}" if b.path else "/#chat")
            result = await self.notifier.notify(title, message, 3, url, dedupe=f"birthday:{b.path or b.name}:{when}:{days}",
                                                tags="birthday", category="birthdays")
            sent += int(result != "duplicate")
        return {"sent": sent}
