"""Read "add X on <date> at <time> to my calendar" by script (Tier 0), so the small model isn't needed for it.

Understands UK and ISO dates in short and long form — 31/10/2026, 31/10/26, 31/10, 31.10.2026, 2026-10-31,
31 October 2026, 31st Oct, Saturday 31 October, October 31, today, tomorrow, (next) Friday — and times
such as 7pm, 7:30pm, 19:00, 7.30pm, noon, ranges (7-9pm, 7pm to 10:30pm, 19:00–21:00), "until 10pm",
"for 2 hours" and "all day". Everything else becomes the title.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .events import EventCandidate

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
      r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
WD = r"(?:mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday|rsday)?\.?"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

COMMAND = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:add|put|pop|stick|schedule|book|create|enter)\b(?:\s+(?:an?|the))?"
    r"(?:\s+event)?|"
    r"\b(?:to|in|into|on)\s+(?:my\s+|the\s+|our\s+)?(?:[\w'’&-]+\s+){0,3}?(?:google\s+)?(?:calendar|diary|cal)\b",
    re.IGNORECASE)
CALENDAR_NAME = re.compile(
    r"\b(?:to|in|into|on)\s+(?:my\s+|the\s+|our\s+)?(?P<name>(?:[\w'’&-]+\s+){1,3}?)(?:google\s+)?"
    r"(?:calendar|diary|cal)\b", re.IGNORECASE)


def calendar_name(prompt: str) -> str:
    """'…to the Family calendar' → 'Family'; '' for my/the calendar."""
    match = CALENDAR_NAME.search(prompt)
    if not match:
        return ""
    name = match.group("name").strip()
    return "" if name.casefold() in {"google", "my", "the", "our", "main", "own"} else name

DATE_PATTERNS = [
    ("iso", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")),
    # (not part of a time like 10:30-11:30 or a range like 7-9pm)
    ("num", re.compile(r"(?<![:.\d])\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{4}|\d{2}))?\b"
                       r"(?![:.]\d)(?!\s*(?:am|pm|a\.m|p\.m))", re.I)),
    ("dmy", re.compile(rf"\b(?:{WD},?\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MON},?(?:\s+(\d{{4}}))?\b", re.I)),
    ("mdy", re.compile(rf"\b(?:{WD},?\s+)?{MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?(?:\s+(\d{{4}}))?\b", re.I)),
    ("word", re.compile(r"\b(today|tonight|tomorrow|the day after tomorrow)\b", re.I)),
    ("weekday", re.compile(rf"\b(?:on\s+)?(next|this)?\s*({WD})\b", re.I)),
]
T = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
RANGE = re.compile(rf"\b(?:from\s+|at\s+|between\s+)?{T}\s*(?:-|–|—|to|until|till|and)\s*{T}(?!\s*[/.\-]\d)", re.I)
SINGLE = [
    re.compile(r"\b(?:at\s+|@\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])", re.I),
    re.compile(r"\b(?:at\s+|@\s*)?([01]?\d|2[0-3])[:.]([0-5]\d)\b(?!\s*[/.\-]\d)"),
    re.compile(r"\bat\s+(\d{1,2})\b(?![:./\-]\d)"),
    re.compile(r"\b(?:at\s+)?(noon|midday|midnight)\b", re.I),
]
UNTIL = re.compile(rf"\b(?:until|till|to|ending at|finishing at)\s+{T}", re.I)
FOR = re.compile(r"\bfor\s+(\d+(?:\.\d+)?|an?|half an?)\s*(hours?|hrs?|h|minutes?|mins?)\b", re.I)
ALL_DAY = re.compile(r"\b(?:all[\s-]day|whole day)\b", re.I)


def _clock(hour: str, minute: str | None, meridiem: str | None) -> time | None:
    h, m = int(hour), int(minute or 0)
    if meridiem:
        mer = meridiem.casefold().replace(".", "")
        if not 1 <= h <= 12:
            return None
        h = (0 if h == 12 else h) + (12 if mer == "pm" else 0)
    if 0 <= h < 24 and 0 <= m < 60:
        return time(h, m)
    return None


def _cut(text: str, match: re.Match) -> str:
    return text[:match.start()] + " " + text[match.end():]


def _find_date(text: str, today: date) -> tuple[date | None, str]:
    for kind, pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                if kind == "iso":
                    found = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                    return found, _cut(text, match)
                if kind == "num":
                    a, b, year = int(match.group(1)), int(match.group(2)), match.group(3)
                    day, month = (a, b) if b <= 12 else (b, a)  # UK day/month; 10/31 is clearly month/day
                    full_year = int(year) + (2000 if len(year) == 2 else 0) if year else None
                    found = date(full_year or today.year, month, day)
                    if not full_year and found < today:
                        found = date(today.year + 1, month, day)
                    return found, _cut(text, match)
                if kind in {"dmy", "mdy"}:
                    if kind == "dmy":
                        day, name, year = int(match.group(1)), match.group(2), match.group(3)
                    else:
                        name, day, year = match.group(1), int(match.group(2)), match.group(3)
                    month = MONTHS.index(name.casefold()[:3]) + 1
                    found = date(int(year) if year else today.year, month, day)
                    if not year and found < today:
                        found = date(today.year + 1, month, day)
                    return found, _cut(text, match)
                if kind == "word":
                    word = match.group(1).casefold()
                    offset = {"today": 0, "tonight": 0, "tomorrow": 1, "the day after tomorrow": 2}[word]
                    return today + timedelta(days=offset), _cut(text, match)
                if kind == "weekday":
                    wanted = WEEKDAYS.index(match.group(2).casefold()[:3])
                    ahead = (wanted - today.weekday()) % 7
                    if ahead == 0 and not (match.group(1) and match.group(1).casefold() == "this"):
                        ahead = 7  # "Tuesday" said on a Tuesday means next week
                    return today + timedelta(days=ahead), _cut(text, match)
            except ValueError:
                continue  # e.g. 31/02 — try the next match
    return None, text


def _find_times(text: str) -> tuple[time | None, time | None, bool, str]:
    """(start, end, all_day, remaining text)."""
    if ALL_DAY.search(text):
        return None, None, True, ALL_DAY.sub(" ", text)
    match = RANGE.search(text)
    if match and (match.group(3) or match.group(6) or match.group(2) or match.group(5)):
        end_mer = match.group(6)
        start_mer = match.group(3) or end_mer
        start = _clock(match.group(1), match.group(2), start_mer)
        end = _clock(match.group(4), match.group(5), end_mer or start_mer)
        if start and end and end <= start and not match.group(3) and end_mer:
            start = _clock(match.group(1), match.group(2), "am")  # "11-1pm" → 11am to 1pm
        if start and end:
            return start, end, False, _cut(text, match)
    for pattern in SINGLE:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if groups[0].casefold() in {"noon", "midday", "midnight"}:
            start = time(0, 0) if groups[0].casefold() == "midnight" else time(12, 0)
        elif len(groups) == 3:
            start = _clock(groups[0], groups[1], groups[2])
        elif len(groups) == 2:
            start = _clock(groups[0], groups[1], None)
        else:
            hour = int(groups[0])
            start = _clock(str(hour + 12 if 1 <= hour <= 7 else hour), None, None)  # "at 7" means 7pm
        if start is None:
            continue
        rest = _cut(text, match)
        end = None
        until = UNTIL.search(rest)
        if until:
            end = _clock(until.group(1), until.group(2), until.group(3) or ("pm" if start.hour >= 12 else None))
            rest = _cut(rest, until)
        return start, end, False, rest
    return None, None, False, text


def _title(text: str) -> str:
    text = COMMAND.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    # connecting words and punctuation left at the edges ("on", "at", ",", "-")
    edge = r"(?:on|at|from|for|by|,|:|;|-|–|—|\.|\bthe\s*$)"
    for _ in range(4):
        text = re.sub(rf"^\s*{edge}\s*|\s*{edge}\s*$", "", text, flags=re.I).strip()
    text = text.strip(" ,:;-–—\"'“”")
    return text[:1].upper() + text[1:] if text else ""


def parse_event_request(prompt: str, now: datetime) -> EventCandidate | None:
    """A calendar event from a chat request, or None if the script can't find a date."""
    tz = now.tzinfo
    text = COMMAND.sub(" ", prompt)
    text = FOR.sub(lambda m: m.group(0).replace(" ", " "), text)  # keep "for 2 hours" together for later
    day, text = _find_date(text, now.date())  # dates first, so 3.11.26 isn't read as 3:11
    if day is None:
        return None
    start_time, end_time, all_day, text = _find_times(text)
    duration = None
    text = text.replace(" ", " ")
    match = FOR.search(text)
    if match and start_time:
        amount = {"a": 1, "an": 1, "half a": 0.5, "half an": 0.5}.get(match.group(1).casefold(), None)
        amount = float(match.group(1)) if amount is None else amount
        duration = timedelta(minutes=amount) if match.group(2).casefold().startswith("m") else timedelta(hours=amount)
        text = _cut(text, match)
    title = _title(text)
    if not title:
        return None
    if start_time is None and not all_day and "tonight" in prompt.casefold():
        start_time = time(19, 0)
    if start_time is None:
        return EventCandidate(title=title, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(),
                              all_day=True, confidence=0.95, source="chat")
    begin = datetime.combine(day, start_time, tz)
    if end_time is not None:
        finish = datetime.combine(day, end_time, tz)
        if finish <= begin:
            finish += timedelta(days=1)  # "10pm-1am"
    else:
        finish = begin + (duration or timedelta(hours=1))
    return EventCandidate(title=title, start=begin.isoformat(), end=finish.isoformat(), all_day=False,
                          confidence=0.95, source="chat")
