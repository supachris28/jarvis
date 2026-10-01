"""Requests that ask for several things at once, split and linked by script.

"Add booking reference is 203BIR-NGC5GHR to bowling on Saturday and remind me of it at 4:30 so I have it ready"
becomes two requests that each go through the normal handlers:
  1. "Add booking reference is 203BIR-NGC5GHR to bowling on Saturday"
  2. "remind me of the bowling booking reference 203BIR-NGC5GHR at 4:30pm on Saturday 3 October so I have it ready"
Later parts borrow what earlier ones were about ("it"), their day, and an afternoon reading of bare times like 4:30.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..extract.event_text import _find_date, _find_times

ACTION = (r"(?:please\s+)?(?:remind\s+me|set\s+(?:a\s+)?reminder|add|put|pop|turn|switch|set|book|schedule|create|"
          r"lock|open|close|note|remember|save|start|run|activate|read)\b")
SPLIT = re.compile(rf"\s*[,;]?\s+(?:and(?:\s+then|\s+also)?|then|also|plus)\s+(?={ACTION})", re.I)
BARE_TIME = re.compile(r"\bat\s+([1-6])(?:([:.])(\d{2}))?\b(?!\s*(?:am|pm|a\.m|p\.m|o'?clock\s+in\s+the\s+morning))", re.I)
PRONOUN = re.compile(r"\b(it|that|them|this)\b", re.I)
ANNOTATE = re.compile(r"^\s*(?:please\s+)?(?:add|put|attach|note|save|stick)\s+(?:the\s+|my\s+)?(?P<what>.+?)\s+"
                      r"(?:to|on|in|against|with)\s+(?:the\s+|my\s+|our\s+)?(?P<target>.+?)\s*[.!]?$", re.I)


def split_request(prompt: str) -> list[str]:
    """Split only when every part starts with an action ('… and remind me …'); otherwise [prompt]."""
    parts = [p.strip(" ,;") for p in SPLIT.split(prompt.strip()) if p and p.strip(" ,;")]
    if len(parts) < 2 or not all(re.match(ACTION, p, re.I) for p in parts):
        return [prompt]
    return parts


def annotation_parts(clause: str) -> tuple[str, str] | None:
    """'Add booking reference is X to bowling on Saturday' → ('booking reference is X', 'bowling on Saturday')."""
    match = ANNOTATE.match(clause)
    if not match:
        return None
    target = match.group("target")
    if re.search(r"\b(calendar|diary|cal|list|notes?|vault|journal)\b", target, re.I):
        return None  # "add … to my calendar" / "to the shopping list" are other things
    return match.group("what").strip(), target.strip()


def detail_line(what: str) -> str:
    """'booking reference is 203BIR-NGC5GHR' → 'Booking reference: 203BIR-NGC5GHR'."""
    match = re.match(r"^(?P<label>.+?)\s+(?:is|=|:)\s+(?P<value>.+)$", what.strip(), re.I)
    text = f"{match.group('label').strip()}: {match.group('value').strip()}" if match else what.strip()
    return text[:1].upper() + text[1:]


def _subject(clause: str, today: date) -> str:
    """What a clause was about, for 'it' in the next one."""
    parts = annotation_parts(clause)
    if parts:
        what, target = parts
        _, rest = _find_date(target, today)
        target = re.sub(r"\s+", " ", re.sub(r"\b(on|at|for|this|next)\b\s*$", "", rest.strip(), flags=re.I)).strip()
        line = detail_line(what)
        return f"the {target} {line[:1].lower() + line[1:]}".replace(":", "") if target else line
    text = re.sub(rf"^{ACTION}\s*", "", clause, flags=re.I)
    _, text = _find_date(text, today)
    text = _find_times(text)[3]
    text = re.sub(r"\b(to|in|on)\s+(my|the)\s+(\w+\s+)?(calendar|diary)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,.")


def link_clauses(parts: list[str], now: datetime) -> list[str]:
    """Give later parts the subject and day of earlier ones, and read bare 1–6 o'clock times as pm."""
    linked: list[str] = []
    day: date | None = None
    subject = ""
    for index, part in enumerate(parts):
        found, _ = _find_date(part, now.date())
        if index and subject and PRONOUN.search(part):
            part = PRONOUN.sub(subject, part, count=1)
        if index:
            part = BARE_TIME.sub(lambda m: f"at {m.group(1)}{(m.group(2) or '') and ':' + m.group(3)}pm", part)
            if found is None and day is not None and re.search(r"\bat\s+\d|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", part, re.I):
                part = re.sub(r"(\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", rf"\1 on {day:%A} {day.day} {day:%B}", part,
                              count=1, flags=re.I)
        day = found or day
        subject = _subject(part, now.date()) or subject
        linked.append(part)
    return linked
