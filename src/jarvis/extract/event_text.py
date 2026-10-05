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


def _find_date(text: str, today: date, kinds: tuple[str, ...] | None = None) -> tuple[date | None, str]:
    """The first date in `text` and the text without it. `kinds` limits which forms are looked for
    (iso, num, dmy, mdy, word, weekday) — reminders use only the calendar-date forms and read days themselves."""
    for kind, pattern in DATE_PATTERNS:
        if kinds is not None and kind not in kinds:
            continue
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


# ---------------------------------------------------------------- several dates in one email (by script)
LIST_SKIP = re.compile(r"[£$€]\s?\d|\b(order|invoice|paid|payment|refund|delivered|dispatched|statement|balance|"
                       r"sent|received|posted|wrote|unsubscribe)\b", re.I)
SUBJECT_NOISE = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", re.I)
WEEKDAY_ONLY = re.compile(rf"^(?:{WD}|and|&|,|\s)*$", re.I)


def _date_spans(text: str, today: date) -> list[tuple[int, int, date]]:
    """Every explicit date in a line: (start, end, date), in order, not overlapping."""
    spans: list[tuple[int, int, date]] = []
    for kind, pattern in DATE_PATTERNS:
        if kind not in ("iso", "num", "dmy", "mdy"):
            continue
        for match in pattern.finditer(text):
            if any(a < match.end() and match.start() < b for a, b, _ in spans):
                continue
            found, _ = _find_date(match.group(0), today, (kind,))
            if found is not None:
                spans.append((match.start(), match.end(), found))
    return sorted(spans)


RANGE_JOIN = re.compile(r"^\s*(?:-|–|—|to|until|till)\s*$", re.I)


def parse_date_list(subject: str, body: str, now: datetime, limit: int = 20) -> list[EventCandidate]:
    """Emails that list several dates — rehearsals, fixtures, term dates, a course's sessions — become one event per
    date. Only explicit dates count (not bare weekdays), on short lines, and there must be at least two different
    future dates; lines about money or orders are ignored. "26/10 - 30/10" is one event over those days; "13/11 7pm and
    14/11 2:30pm" is two. The title is the line's own words, after the email's subject."""
    tz = now.tzinfo
    today = now.date()
    base = SUBJECT_NOISE.sub("", subject or "").strip(" -:") or "Event"
    found: list[EventCandidate] = []
    seen: set[str] = set()

    def add(day: date, last: date | None, text: str, label: str) -> None:
        if day < today or day > today + timedelta(days=400) or len(found) >= limit:
            return
        start, end, _, rest = _find_times(text)
        own = _title(re.sub(r"[()\[\]]", " ", f"{label} {rest}"))
        own = re.sub(r"(?:\s*(?:\band\b|&|,|;|\bKO\b|kick[- ]?off|starts?|start time|from|at)\s*)+$", "", own,
                     flags=re.I).strip(" :-,") if own else ""
        if own and WEEKDAY_ONLY.match(own):
            own = ""
        title = f"{base} — {own[:1].upper() + own[1:]}" if own and own.casefold() not in base.casefold() else base
        if last is not None or start is None:
            finish_day = (last or day) + timedelta(days=1)
            event = EventCandidate(title=title[:200], start=day.isoformat(), end=finish_day.isoformat(),
                                   all_day=True, confidence=0.8, source="list")
        else:
            begin = datetime.combine(day, start, tz)
            finish = datetime.combine(day, end, tz) if end else begin + timedelta(hours=1)
            if finish <= begin:
                finish += timedelta(days=1)
            event = EventCandidate(title=title[:200], start=begin.isoformat(), end=finish.isoformat(), all_day=False,
                                   confidence=0.8, source="list")
        if event.start not in seen:
            seen.add(event.start)
            found.append(event)

    for raw in body.splitlines():
        line = raw.strip(" \t-•*·–—")
        if not line or len(line) > 200 or LIST_SKIP.search(line) or line.startswith("---"):
            continue
        spans = _date_spans(line, today)
        if not spans:
            continue
        label = line[:spans[0][0]]          # "Autumn half term:", "Performances:"
        index = 0
        while index < len(spans):
            a, b, day = spans[index]
            if index + 1 < len(spans) and RANGE_JOIN.match(line[b:spans[index + 1][0]]):
                add(day, spans[index + 1][2], "", label)            # a range of days
                index += 2
                continue
            tail_end = spans[index + 1][0] if index + 1 < len(spans) else len(line)
            add(day, None, line[b:tail_end], label)
            index += 1
    return found if len({e.start[:10] for e in found}) >= 2 else []
