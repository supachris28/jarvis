"""'What's in my calendar tomorrow?' answered by script (Tier 0).

The day (or days) asked about is read from the question, events are taken from the calendar the calendar job keeps
(or Google, beyond that window), filtered to exactly those days in local time (BST/GMT, not UTC) and listed as-is,
so the model can neither pick up events from the wrong day nor print UTC times.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo

from ..extract.event_text import _find_date
from ..extract.events import parse_iso

AGENDA_QUESTION = re.compile(
    r"\b(calendar|diary|schedule|agenda|appointments?|events?|meetings?|plans?|planned|happening|busy|free|"
    r"booked|am i doing|are we doing|up to|my (?:day|week|weekend))\b|"
    r"\b(?:what'?s|what is|anything|something|is there anything|have i got|have we got|do i have|do we have|"
    r"what have i got|what do i have)\s+(?:else\s+)?(?:on|going on|planned)\b|^\s*what have (?:i|we) got\b", re.I)
NOT_AGENDA = re.compile(r"\b(weather|rain(?:ing)?|forecast|temperature|tv|telly|film|bins?|tide|sunrise|sunset|"
                        r"email|emails|inbox|parcel|delivery|deliveries)\b", re.I)
ASKING = re.compile(
    r"^\s*(?:hey\s+|ok\s+)?(?:jarvis[,\s]+)?(?:what|what's|whats|anything|any|is|am|are|do|have|has|show|list|check|"
    r"read|tell|give|when|how)\b|\?\s*$", re.I)
PERIODS = [
    (re.compile(r"\b(?:this|the)\s+weekend\b", re.I), "this weekend"),
    (re.compile(r"\bnext\s+weekend\b", re.I), "next weekend"),
    (re.compile(r"\b(?:this|the)\s+week\b|\brest of (?:the|this) week\b", re.I), "this week"),
    (re.compile(r"\bnext\s+week\b", re.I), "next week"),
    (re.compile(r"\bnext\s+(\d{1,2})\s+days\b", re.I), "next days"),
]


def asked_days(prompt: str, today: date) -> tuple[date, date, str] | None:
    """(first day, day after the last, label) for the days a question is about, or None."""
    for pattern, kind in PERIODS:
        match = pattern.search(prompt)
        if not match:
            continue
        monday = today - timedelta(days=today.weekday())
        if kind == "this weekend":
            saturday = monday + timedelta(days=5)
            start = max(today, saturday)
            return start, monday + timedelta(days=7), "this weekend"
        if kind == "next weekend":
            saturday = monday + timedelta(days=12)
            return saturday, saturday + timedelta(days=2), "next weekend"
        if kind == "this week":
            return today, monday + timedelta(days=7), "the rest of this week"
        if kind == "next week":
            return monday + timedelta(days=7), monday + timedelta(days=14), "next week"
        days = max(1, min(31, int(match.group(1))))
        return today, today + timedelta(days=days), f"the next {days} days"
    found, _ = _find_date(prompt, today)
    if found is None:
        return None
    if found == today:
        label = "today"
    elif found == today + timedelta(days=1):
        label = "tomorrow"
    else:
        label = ""
    return found, found + timedelta(days=1), label


def is_agenda_question(prompt: str, today: date) -> bool:
    """A question about what's on for a day or period ('what's in my calendar tomorrow?', 'am I free Saturday?')."""
    return bool(ASKING.search(prompt) and AGENDA_QUESTION.search(prompt) and not NOT_AGENDA.search(prompt)
                and asked_days(prompt, today))


def _span(event: dict, tz: tzinfo) -> tuple[datetime, datetime, bool]:
    """Local start and end (end exclusive) of an event; all-day events run from midnight to midnight."""
    all_day = bool(event.get("all_day")) or len(str(event.get("start", ""))) == 10
    start = parse_iso(str(event["start"]), tz).astimezone(tz)
    end_text = str(event.get("end") or "")
    end = parse_iso(end_text, tz).astimezone(tz) if end_text else start + (timedelta(days=1) if all_day else
                                                                            timedelta(hours=1))
    if all_day:
        start = datetime.combine(start.date(), time(0), tz)
        end = datetime.combine(end.date(), time(0), tz)
        if end <= start:
            end = start + timedelta(days=1)
    return start, max(end, start), all_day


def events_between(events: list[dict], first: date, after_last: date, tz: tzinfo) -> list[dict]:
    """Events that overlap the local days [first, after_last), each with local times added, in order."""
    window_start = datetime.combine(first, time(0), tz)
    window_end = datetime.combine(after_last, time(0), tz)
    found: dict[str, dict] = {}
    for event in events:
        if event.get("status") == "cancelled" or not event.get("start"):
            continue
        try:
            start, end, all_day = _span(event, tz)
        except ValueError:
            continue
        timed_point = not all_day and end == start
        if not (start < window_end and (end > window_start or (timed_point and start >= window_start))):
            continue
        key = event.get("event_id") or f"{event.get('summary')}|{event['start']}"
        found[key] = event | {"local_start": start, "local_end": end, "all_day": all_day}
    return sorted(found.values(), key=lambda e: (e["local_start"].date(), not e["all_day"], e["local_start"],
                                                 e.get("summary", "")))


def _last_day(event: dict) -> date:
    start, end = event["local_start"], event["local_end"]
    if end <= start:
        return start.date()
    return (end - timedelta(microseconds=1)).date()  # ends at midnight → the day before


def _line(event: dict, day: date) -> str:
    start, end = event["local_start"], event["local_end"]
    title = re.sub(r"\s+", " ", event.get("summary") or "(no title)").strip()
    where = f" — {event['location'].strip()}" if (event.get("location") or "").strip() else ""
    if event["all_day"]:
        last = _last_day(event)
        when = "All day"
        if start.date() < day or last > day:
            when += f" (until {last:%a} {last.day} {last:%b})" if last > day else " (last day)"
        return f"- {when}: {title}{where}"
    if start.date() < day:
        when = f"Until {end:%H:%M}" if _last_day(event) == day else "All day"
        return f"- {when} (from {start:%a} {start.day} {start:%b}): {title}{where}"
    when = f"{start:%H:%M}"
    if end > start:
        when += f"–{end:%H:%M}" if end.date() == start.date() else f" until {end:%a} {end.day} {end:%b} {end:%H:%M}"
    return f"- {when} {title}{where}"


def agenda_text(events: list[dict], first: date, after_last: date, label: str, today: date) -> str:
    """The answer: one heading per day, events in time order, local times."""
    days = [first + timedelta(days=n) for n in range((after_last - first).days)]
    lines: list[str] = []
    for day in days:
        on_day = [e for e in events if e["local_start"].date() <= day <= _last_day(e)]
        heading = f"{day:%A} {day.day} {day:%B}"
        if day == today:
            heading = f"Today, {heading}"
        elif day == today + timedelta(days=1):
            heading = f"Tomorrow, {heading}"
        if len(days) > 1 and not on_day:
            continue  # a week view lists only days with something on
        lines.append(f"**{heading}**")
        lines += [_line(e, day) for e in on_day] or ["- Nothing in your calendar."]
        lines.append("")
    if not any(line.startswith("- ") and "Nothing in your calendar" not in line for line in lines):
        if label:
            return f"Nothing in your calendar {label}."
        if len(days) == 1:
            return f"Nothing in your calendar on {first:%A} {first.day} {first:%B}."
        return f"Nothing in your calendar {first:%a} {first.day} {first:%b} – {days[-1]:%a} {days[-1].day} {days[-1]:%b}."
    return "\n".join(lines).strip()
