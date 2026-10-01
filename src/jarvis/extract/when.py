"""Deterministic parsing of times like "in 20 minutes", "tomorrow at 9", "Friday 3pm",
"every weekday at 7am", "on 14 October at 18:30". No LLM involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD = r"(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday|rsday)?"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"


@dataclass
class When:
    at: datetime | None       # None when no time expression was found
    repeat: str               # '' | daily | weekdays | weekly | monthly
    rest: str                 # the text with the time expression removed
    explicit_time: bool


def _weekday_index(token: str) -> int:
    token = token.casefold()[:3]
    return [w[:3] for w in WEEKDAYS].index(token)


def _clock(hour: int, minute: int, meridiem: str | None) -> time | None:
    if meridiem:
        meridiem = meridiem.casefold().replace(".", "")
        if hour == 12:
            hour = 0
        if meridiem.startswith("p"):
            hour += 12
    if 0 <= hour < 24 and 0 <= minute < 60:
        return time(hour, minute)
    return None


PATTERNS = [
    # relative: in 20 minutes / in 2 hours / in 3 days
    ("relative", re.compile(r"\bin\s+(an?|\d+(?:\.\d+)?)\s*(min(?:ute)?s?|hours?|hrs?|days?|weeks?)\b", re.I)),
    ("repeat_weekday", re.compile(rf"\bevery\s+{WD}\b", re.I)),
    ("repeat", re.compile(r"\b(every\s+day|daily|every\s+weekday|weekdays|on\s+weekdays|every\s+week|weekly|"
                          r"every\s+month|monthly|every\s+morning|every\s+evening|every\s+night)\b", re.I)),
    ("date_dm", re.compile(rf"\b(?:on\s+)?(?:{WD}\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MON}\b", re.I)),
    ("date_md", re.compile(rf"\b(?:on\s+)?{MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)),
    ("date_num", re.compile(r"\b(?:on\s+)?(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")),
    ("day_word", re.compile(r"\b(today|tonight|this\s+evening|this\s+afternoon|this\s+morning|tomorrow(?:\s+(?:morning|afternoon|evening|night))?)\b", re.I)),
    ("weekday", re.compile(rf"\b(?:on\s+|next\s+|this\s+)?{WD}\b", re.I)),
    ("clock", re.compile(r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b|\b(?:at\s+)?(\d{1,2})[:.](\d{2})\b|"
                         r"\bat\s+(\d{1,2})\b(?!\s*(?:%|st|nd|rd|th|/))|\b(?:at\s+)?(noon|midday|midnight)\b", re.I)),
]

PART_OF_DAY = {"morning": time(9, 0), "afternoon": time(14, 0), "evening": time(19, 0), "night": time(20, 0),
               "tonight": time(20, 0)}


def parse_when(text: str, now: datetime) -> When:
    """Find a time expression in `text` (relative to `now`, which must be timezone-aware)."""
    tz = now.tzinfo
    remaining = text
    target_date: date | None = None
    clock: time | None = None
    repeat = ""
    relative: timedelta | None = None
    default_clock: time | None = None
    weekday_repeat: int | None = None

    def cut(match: re.Match) -> None:
        nonlocal remaining
        remaining = remaining[:match.start()] + " " + remaining[match.end():]

    for kind, pattern in PATTERNS:
        match = pattern.search(remaining)
        if not match:
            continue
        if kind == "relative":
            amount = 1.0 if match.group(1).casefold() in {"a", "an"} else float(match.group(1))
            unit = match.group(2).casefold()
            if unit.startswith("min"):
                relative = timedelta(minutes=amount)
            elif unit.startswith(("hour", "hr")):
                relative = timedelta(hours=amount)
            elif unit.startswith("day"):
                relative = timedelta(days=amount)
            else:
                relative = timedelta(weeks=amount)
        elif kind == "repeat_weekday":
            weekday_repeat = _weekday_index(match.group(1))
            repeat = "weekly"
        elif kind == "repeat":
            phrase = match.group(1).casefold()
            if "weekday" in phrase:
                repeat = "weekdays"
            elif "week" in phrase:
                repeat = "weekly"
            elif "month" in phrase:
                repeat = "monthly"
            else:
                repeat = "daily"
                for part, value in PART_OF_DAY.items():
                    if part in phrase:
                        default_clock = value
        elif kind in {"date_dm", "date_md", "date_num"}:
            if target_date is not None:
                continue
            try:
                if kind == "date_dm":
                    day, month = int(match.group(2)), MONTHS.index(match.group(3).casefold()[:3]) + 1
                    year = now.year
                elif kind == "date_md":
                    month, day = MONTHS.index(match.group(1).casefold()[:3]) + 1, int(match.group(2))
                    year = now.year
                else:  # UK order: day/month
                    day, month = int(match.group(1)), int(match.group(2))
                    year = int(match.group(3)) if match.group(3) else now.year
                    year = year + 2000 if year < 100 else year
                target_date = date(year, month, day)
                if target_date < now.date() and not (kind == "date_num" and match.group(3)):
                    target_date = date(year + 1, month, day)
            except ValueError:
                continue
        elif kind == "day_word":
            if target_date is not None:
                continue
            word = match.group(1).casefold()
            target_date = now.date() + timedelta(days=1) if word.startswith("tomorrow") else now.date()
            for part, value in PART_OF_DAY.items():
                if part in word:
                    default_clock = value
        elif kind == "weekday":
            if target_date is not None or weekday_repeat is not None:
                continue
            wanted = _weekday_index(match.group(1))
            ahead = (wanted - now.weekday()) % 7
            if ahead == 0 or "next" in match.group(0).casefold():
                ahead = ahead or 7
            target_date = now.date() + timedelta(days=ahead)
        elif kind == "clock":
            if match.group(7):
                word = match.group(7).casefold()
                clock = time(0, 0) if word == "midnight" else time(12, 0)
            elif match.group(1):
                clock = _clock(int(match.group(1)), int(match.group(2) or 0), match.group(3))
            elif match.group(4):
                clock = _clock(int(match.group(4)), int(match.group(5)), None)
            else:
                hour = int(match.group(6))
                # "at 7" with no am/pm: assume the next sensible time (7 → 07:00 before noon, else 19:00)
                clock = _clock(hour + 12 if 1 <= hour <= 7 else hour, 0, None)
            if clock is None:
                continue
        cut(match)

    rest = re.sub(r"\s+", " ", remaining).strip(" ,.;:-")
    explicit = clock is not None
    if relative is not None:
        return When(now + relative, repeat, rest, True)
    if weekday_repeat is not None:
        ahead = (weekday_repeat - now.weekday()) % 7
        target_date = now.date() + timedelta(days=ahead)
    if target_date is None and clock is None and default_clock is None and not repeat:
        return When(None, "", text.strip(), False)
    clock = clock or default_clock or time(9, 0)
    if target_date is None:
        candidate = datetime.combine(now.date(), clock, tz)
        if candidate <= now:
            candidate += timedelta(days=1)
    else:
        candidate = datetime.combine(target_date, clock, tz)
        if candidate <= now and weekday_repeat is not None:
            candidate += timedelta(days=7)
    if repeat == "weekdays":
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return When(candidate, repeat, rest, explicit)


def next_occurrence(previous: datetime, repeat: str) -> datetime | None:
    if repeat == "daily":
        return previous + timedelta(days=1)
    if repeat == "weekly":
        return previous + timedelta(weeks=1)
    if repeat == "weekdays":
        candidate = previous + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    if repeat == "monthly":
        month = previous.month % 12 + 1
        year = previous.year + (1 if month == 1 else 0)
        for day in (previous.day, 30, 29, 28):
            try:
                return previous.replace(year=year, month=month, day=day)
            except ValueError:
                continue
    return None


def describe(at: datetime | None, repeat: str, now: datetime) -> str:
    if at is None:
        return "now"
    labels = {"daily": "every day", "weekdays": "every weekday", "weekly": f"every {at:%A}", "monthly": "every month"}
    if repeat:
        return f"{labels.get(repeat, repeat)} at {at:%H:%M} (next {at:%a %d %b})"
    if at.date() == now.date():
        return f"today at {at:%H:%M}"
    if at.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {at:%H:%M}"
    if at - now < timedelta(days=7):
        return f"{at:%A} at {at:%H:%M}"
    return f"{at:%a %d %b %Y} at {at:%H:%M}"
