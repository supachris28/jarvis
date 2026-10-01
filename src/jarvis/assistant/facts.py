"""Scripted lookups for facts about people (Tier 0 — no AI).

Birthdays can live in several places, written in several ways:
- Google Contacts (synced into the `contacts` table)
- a People note's properties: birthday / birthdate / born / dob / bday / date of birth
- a line in a People note: "Birthday: 12 March", "- Born: 12/03/1985"
- anywhere else in the vault: "Jen's birthday is 3 May" (e.g. a "remember that …" capture)

`collect_birthdays` gathers them all into one list so questions such as "when is Jen's birthday?" or
"any birthdays coming up?" are answered from data rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..vault.markdown import split_frontmatter

BIRTHDAY_QUESTION = re.compile(r"\b(birthdays?|b-?days?|born|how old|turns? \d+|turning \d+|age of|date of birth|dob)\b",
                               re.IGNORECASE)
BIRTHDAY_KEYS = ("birthday", "birthdate", "birth_date", "born", "dob", "bday", "date_of_birth", "date of birth")
MONTHS = {m: i for i, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], 1) for m in names}
MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_FORMS = (
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),                               # 1985-03-12
    re.compile(r"^--(?P<m>\d{1,2})-(?P<d>\d{1,2})"),                                          # --03-12 (no year)
    re.compile(rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+(?P<mon>{MONTH})\.?,?(?:\s+(?P<y>\d{{4}}))?", re.I),
    re.compile(rf"(?P<mon>{MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(?P<y>\d{{4}}))?", re.I),
    re.compile(r"(?P<d>\d{1,2})[/.](?P<m>\d{1,2})(?:[/.](?P<y>\d{4}|\d{2}))?\b"),             # 12/03/1985 (UK order)
)
LINE = re.compile(r"^\s*(?:[-*+]\s*)?(?:\*\*|__)?(birthday|birthdate|born|dob|date of birth|bday)(?:\*\*|__)?\s*"
                  r"(?:\*\*|__)?\s*[:：=–-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
MENTION = re.compile(r"(?P<name>[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)(?:'s|’s)\s+(?:birthday|bday|b-day)\s+(?:is\s+)?"
                     r"(?:on\s+)?(?:the\s+)?(?P<when>[^.\n;]{3,40})", re.UNICODE)


def parse_date(value) -> tuple[int | None, int, int] | None:
    """Loose birthday parser → (year or None, month, day)."""
    if isinstance(value, date):
        return value.year, value.month, value.day
    text = str(value or "").strip()
    if not text:
        return None
    for form in DATE_FORMS:
        match = form.search(text)
        if not match:
            continue
        parts = match.groupdict()
        if parts.get("mon"):
            name = parts["mon"].casefold().rstrip(".")
            month = MONTHS.get(name) or MONTHS.get(name[:3], 0)
        else:
            month = int(parts["m"])
        day = int(parts["d"])
        year = parts.get("y")
        if year:
            year = int(year) if len(year) == 4 else int(year) + (1900 if int(year) > 30 else 2000)
        try:
            date(2000, month, day)  # 2000 is a leap year, so 29 Feb is allowed
        except ValueError:
            continue
        return year or None, month, day
    return None


@dataclass
class Birthday:
    name: str
    path: str
    month: int
    day: int
    year: int | None = None
    sources: list[str] = field(default_factory=list)
    relation: str = ""

    def next_date(self, today: date) -> date:
        for year in (today.year, today.year + 1):
            try:
                when = date(year, self.month, self.day)
            except ValueError:
                when = date(year, 3, 1)  # 29 February in a non-leap year
            if when >= today:
                return when
        return date(today.year + 1, self.month, min(self.day, 28))

    def describe(self, today: date) -> str:
        when = self.next_date(today)
        days = (when - today).days
        away = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        born = f"{date(2000, self.month, self.day):%d %B}"
        if self.year:
            born += f" {self.year}"
        turns = f", turns {when.year - self.year}" if self.year else ""
        where = f" [{self.path}]" if self.path else ""
        who = f"{self.name} ({self.relation})" if self.relation else self.name
        return f"{who}: born {born} — next birthday {when:%a %d %b %Y} ({away}{turns}){where} " \
               f"(from {', '.join(self.sources)})"


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


async def collect_birthdays(db, vault, people_index: list[tuple[str, str]]) -> tuple[list[Birthday], list[str]]:
    """All birthdays Jarvis can find, plus loose mentions it couldn't parse (for the model to read)."""
    found: dict[str, Birthday] = {}
    unparsed: list[str] = []
    path_names = {p: n for n, p in reversed(people_index)}  # the longest name per path wins

    def add(name: str, path: str, parsed, source: str) -> None:
        if not parsed:
            return
        year, month, day = parsed
        key = path or _key(name)
        entry = found.get(key)
        if entry is None:
            found[key] = Birthday(path_names.get(path, name), path, month, day, year, [source])
            return
        if source not in entry.sources:
            entry.sources.append(source)
        if year and not entry.year and (entry.month, entry.day) == (month, day):
            entry.year = year

    # 1. Google Contacts
    for row in db.all("SELECT name, path, birthday FROM contacts WHERE birthday != ''"):
        add(row["name"], row["path"] or "", parse_date(row["birthday"]), "Google Contacts")
    # 2 + 3. People notes: properties and "Birthday: …" lines
    people_notes = getattr(vault, "people_notes", None)
    paths = []
    if people_notes is not None:
        try:
            paths = [(path, title) for path, title, _ in await people_notes()]
        except Exception:  # noqa: BLE001 — vault offline: contacts still answer
            paths = []
    for path, title in paths:
        try:
            text = await vault.get_text(path)
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        frontmatter, body = split_frontmatter(text)
        frontmatter = frontmatter or {}
        relation = frontmatter.get("relation") or frontmatter.get("relationship") or ""
        relation = ", ".join(map(str, relation)) if isinstance(relation, list) else str(relation)
        for key, value in frontmatter.items():
            if str(key).casefold().replace("-", "_") in BIRTHDAY_KEYS or str(key).casefold() in BIRTHDAY_KEYS:
                parsed = parse_date(value)
                add(title, path, parsed, "note property")
                if not parsed and value:
                    unparsed.append(f"{title}: {key}: {value} [{path}]")
        for match in LINE.finditer(body):
            parsed = parse_date(match.group(2))
            add(title, path, parsed, "note text")
            if not parsed:
                unparsed.append(f"{title}: {match.group(0).strip()[:120]} [{path}]")
        if relation and path in found:
            found[path].relation = relation[:60]
    # 4. mentions anywhere else ("Jen's birthday is 3 May")
    search = getattr(vault, "search_simple", None)
    if search is not None:
        try:
            hits = await search("birthday")
        except Exception:  # noqa: BLE001
            hits = []
        names = {_key(n): p for n, p in people_index}
        for hit in hits[:25]:
            path = hit.get("filename", "")
            if path.startswith("People/"):
                continue
            text = await vault.get_text(path) or ""
            for match in MENTION.finditer(text):
                name = match.group("name")
                person = names.get(_key(name), "")
                parsed = parse_date(match.group("when"))
                add(name, person, parsed, f"mentioned in {path}")
                if not parsed:
                    unparsed.append(f"{match.group(0).strip()[:120]} [{path}]")
            for line in text.splitlines():
                if re.search(r"birthday", line, re.I) and not MENTION.search(line) and len(unparsed) < 40:
                    unparsed.append(f"{line.strip()[:160]} [{path}]")
    return list(found.values()), unparsed[:40]


def birthday_context(birthdays: list[Birthday], unparsed: list[str], named_paths: list[str], names_in_prompt: str,
                     today: date) -> str:
    """Text for the model: the named people's birthdays, or everyone's sorted by the next one."""
    wanted = [b for b in birthdays if b.path in named_paths] if named_paths else []
    if not wanted and names_in_prompt:
        lowered = names_in_prompt.casefold()
        wanted = [b for b in birthdays if any(len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", lowered)
                                              for part in b.name.casefold().split())]
    rows = wanted or sorted(birthdays, key=lambda b: b.next_date(today))
    lines = [f"Today is {today:%A %d %B %Y}.",
             f"{'Birthdays of the people asked about' if wanted else 'All known birthdays, soonest first'} "
             f"({len(rows)} of {len(birthdays)} known):"]
    lines += [f"- {b.describe(today)}" for b in rows[:60]]
    if not rows:
        lines.append("- (none found in Google Contacts or People notes)")
    if unparsed:
        lines.append("Other birthday mentions found in the vault (dates not understood by script):")
        lines += [f"- {u}" for u in unparsed]
    return "\n".join(lines)
