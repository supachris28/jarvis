"""Request routing: cheap deterministic checks first, then the local router model."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

ROUTES = ("chat", "vault", "gmail", "calendar", "drive", "home", "web", "remember")

PLANNER_PROMPT = """
You are Jarvis's request router. Return ONLY one JSON object:
{"route":"chat|vault|gmail|calendar|drive|home|web","query":"short search query"}

vault: the user's personal knowledge in Obsidian — notes, people, family, friends, places,
       things previously remembered, tags (#tag), journal, or anything about the user's life.
gmail: email, messages, senders, inbox.
calendar: meetings, appointments, events, availability, dates, schedule.
drive: Google Drive files, documents, spreadsheets.
home: anything in Chris's house from Home Assistant — any device, room or sensor reading: lights, heating,
      hot water / cylinder, temperatures, humidity, energy and power use, solar, batteries, doors, windows, locks,
      alarms, appliances, cars on charge, bins. "in my home/house" or "at home" means home.
web: questions about the outside world that need facts or anything that may have changed — news, prices,
     sport results, weather elsewhere, opening times, businesses, people in the public eye, products,
     travel, how-to, definitions, "what is/who is/when did" questions about public things.
chat: casual conversation, opinions, writing or brainstorming help, maths, or questions about Jarvis itself.

For web, the query is a good search-engine query (no personal details).

The query keeps names, dates, #tags and key terms. For chat use "".
""".strip()

REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:remember|note(?:\s+down)?|save|jot\s+down)(?:\s+that)?[:,]?\s+(.+)$",
                      re.IGNORECASE | re.DOTALL)
WRITE_ACTION = re.compile(
    r"\b(send|reply|forward|delete|remove|rename|invite|cancel)\b|"
    r"\b(schedule|move|update|share|create|book)\s+(?:a|an|the|this|that|my|our|it|me)\b",
    re.IGNORECASE,
)


CALENDAR_ADD = re.compile(
    r"\b(add|put|pop|stick|schedule|book|create|enter)\b.{0,200}\b(to|in|into|on)\s+(my\s+|the\s+|our\s+)?"
    r"([\w'’&-]+\s+){0,3}?(google\s+)?(calendar|diary|cal)\b", re.IGNORECASE | re.DOTALL)  # "…to the Family calendar"
EMAIL_WORDS = re.compile(r"\b(e-?mails?|inbox|mail|gmail)\b", re.IGNORECASE)
EVENT_WORDS = re.compile(r"\b(events?|dates?|appointments?|bookings?|calendar|invites?|invitations?)\b", re.IGNORECASE)
SCAN_A = re.compile(r"\b(check|scan|look|search|go through)\b.{0,60}\b(e-?mails?|inbox|mail)\b.{0,60}\bfor\b",
                    re.IGNORECASE)
SCAN_B = re.compile(r"\b(events?|dates?|appointments?|bookings?|invitations?)\b.{0,40}\b(in|from)\s+(my\s+)?"
                    r"(e-?mails?|inbox|mail)\b", re.IGNORECASE)
SCAN_C = re.compile(r"\b(add|put)\b.{0,60}\bcalendar\b", re.IGNORECASE)


def is_calendar_add(prompt: str) -> bool:
    return bool(CALENDAR_ADD.search(prompt)) and not EMAIL_WORDS.search(prompt)


def is_event_scan(prompt: str) -> bool:
    """'Check my email for events', 'any appointments in my inbox?', 'anything in my email to add to my calendar'."""
    if SCAN_A.search(prompt) and EVENT_WORDS.search(prompt):
        return True
    if SCAN_B.search(prompt) and re.search(r"\b(any|check|find|scan|what|which|add|calendar)\b", prompt, re.I):
        return True
    return bool(EMAIL_WORDS.search(prompt) and SCAN_C.search(prompt))


@dataclass(frozen=True)
class Plan:
    route: str
    query: str


def remember_text(prompt: str) -> str | None:
    match = REMEMBER.match(prompt)
    return match.group(1).strip() if match else None


def is_write_request(prompt: str) -> bool:
    return bool(WRITE_ACTION.search(prompt))


def parse_plan(raw: str) -> Plan | None:
    candidate = raw.strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except ValueError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    route = str(value.get("route", "")).casefold()
    query = value.get("query", "")
    if route == "obsidian":
        route = "vault"
    if route not in ROUTES or route == "remember" or not isinstance(query, str):
        return None
    if route != "chat" and not query.strip():
        return None
    return Plan(route, query.strip())


EMAIL_LOOK = re.compile(
    r"\b(?:look|search|check|scan|go\s+through|find|dig|have\s+a\s+look|hunt)\b(?:\s+\w+){0,3}?\s+(?:in|through|on|at)?\s*"
    r"(?:my|the|our)?\s*(?:e-?mails?|inbox|gmail|mail)\b\s*(?P<how>for|about|re|regarding|from|to\s+find)\s+(?P<q>.+)",
    re.IGNORECASE | re.DOTALL)
EMAIL_ABOUT = re.compile(
    r"\b(?:any|find|show|list|get|what|which|latest|recent|last)\b(?:\s+\w+){0,2}?\s+e-?mails?\s+"
    r"(?P<how>about|from|re|regarding|on|mentioning)\s+(?P<q>.+)", re.IGNORECASE | re.DOTALL)
GENERIC_EMAIL_WORDS = frozenset("""notice notices email emails message messages update updates info information details
detail anything stuff news things thing latest recent mail mails letter letters week weekend today tomorrow
tonight month next""".split())


EMAIL_FOLLOW_UP = re.compile(
    r"^\s*(?:(?:can|could|will) you\s+|please\s+|ok\s+|no[,.]?\s+|then\s+)*(?:look|search|check|try|have a look)"
    r"(?:\s+(?:in|through|at))?\s+(?:my\s+|the\s+)?(?:e-?mails?|inbox|gmail|mail)"
    r"(?:\s+(?:for\s+)?(?:it|that|this|them|those|instead|too|then|as well|please))*\s*[?.!]*\s*$", re.IGNORECASE)


EMAIL_FROM = re.compile(
    r"\b(?:look|search|find|check|get|show|open|read|what(?:'s| is| was| did)?|any)\b.{0,40}?\be-?mails?\b\s+"
    r"(?:from|by|sent by)\s+(?P<q>.+)", re.IGNORECASE | re.DOTALL)
RECENT_EMAIL = re.compile(r"\b(?:most recent|latest|last|newest|recent)\b", re.IGNORECASE)


def _tidy(text: str) -> str:
    text = re.sub(r"[?.!]+\s*$", "", text).strip()
    text = re.sub(r"\s+(?:please|thanks|thank you)$", "", text, flags=re.I)
    return re.sub(r"^(?:any|all|some|the|my|our|please)\s+", "", text, flags=re.I).strip()


def email_query(who: str = "", topic: str = "") -> str:
    who, topic = _tidy(who), _tidy(topic)
    sender = (f'from:"{who}"' if " " in who else f"from:{who}") if who else ""
    return " ".join(x for x in (sender, topic) if x)


def explicit_email(prompt: str) -> str | None:
    """'Look in my email for life group notices', 'the latest email from Lucy about life group' → a Gmail search,
    without asking the model (which sometimes just chats instead)."""
    if EMAIL_FOLLOW_UP.match(prompt):
        return ""             # "search my email (for it)": about the question before
    prompt = re.sub(r"\b(from|about|for)\.(?=\w)", r"\1 ", prompt, flags=re.I)    # "from.lucy" (a phone typo)
    match = EMAIL_FROM.search(prompt)
    how = "from" if match else ""
    if not match:
        match = EMAIL_LOOK.search(prompt) or EMAIL_ABOUT.search(prompt)
        if not match:
            return None
        how = match.group("how").casefold()
    query = _tidy(match.group("q"))
    if re.fullmatch(r"(?:it|that|this|them|those)", query, re.I):
        return ""
    lead = re.match(r"(?:anything|something|stuff|e-?mails?|messages?|the\s+\w+\s+e-?mail)\s+"
                    r"(from|about|re|regarding)\s+(.+)", query, re.I)
    if lead:                       # "for anything from the council", "for the latest email from Lucy"
        how, query = lead.group(1).casefold(), lead.group(2)
    if not query:
        return None
    if how == "from":              # "Lucy Kitchin about life group notices" → sender + topic
        parts = re.split(r"\s+(?:about|re|regarding|on the subject of|for)\s+", query, maxsplit=1, flags=re.I)
        who, topic = parts[0], parts[1] if len(parts) > 1 else ""
        return email_query(who, topic) or None
    return _tidy(query) or None


def gmail_fallbacks(query: str) -> list[str]:
    """Looser searches when the exact one finds nothing: 'life group notices' → 'life group' (Gmail doesn't match
    plurals or words that only describe the email)."""
    if ":" in query:
        return []
    words = query.split()
    out = []
    core = [w for w in words if w.casefold().strip("'\"") not in GENERIC_EMAIL_WORDS]
    if core and core != words:
        out.append(" ".join(core))
    singular = [re.sub(r"(?<=[a-z]{3})s$", "", w) for w in (core or words)]
    if singular != (core or words):
        out.append(" ".join(singular))
    return out


WEB_EXPLICIT = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:search(?:\s+(?:the\s+)?(?:web|internet|online))?(?:\s+for)?|"
    r"look\s+up|google|find\s+(?:out|online)|check\s+online(?:\s+for)?)\s+(.+?)\s*\??$",
    re.IGNORECASE | re.DOTALL)


def explicit_web(prompt: str) -> str | None:
    """'search the web for X', 'look up X', 'google X' → X."""
    if re.search(r"\b(my|our)\s+(e-?mails?|inbox|notes?|vault|calendar|drive|files?)\b", prompt, re.I):
        return None
    match = WEB_EXPLICIT.match(prompt)
    return match.group(1).strip() if match else None


KEYWORDS = (
    ("calendar", ("calendar", "meeting", "meetings", "appointment", "schedule", "availability", "event", "events",
                  "today", "tomorrow", "this week", "next week")),
    ("home", ("lights on", "light on", "heating", "temperature in", "thermostat", "door open", "is the door",
              "garage", "locked", "sensor", "bin day", "bins", "in my home", "in my house", "at home", "in the house",
              "hot water", "cylinder", "humidity", "energy use", "power use", "solar", "boiler")),
    ("gmail", ("email", "e-mail", "inbox", "gmail", "mail from", "emailed")),
    ("drive", ("google drive", "drive", "spreadsheet", "document", "doc ")),
    ("vault", ("obsidian", "vault", "note", "notes", "#", "remember", "who is", "who's", "birthday", "my wife",
               "my husband", "my son", "my daughter", "my mum", "my dad", "my mom", "friend", "family", "journal")),
    ("web", ("latest", "news", "price of", "how much is", "who won", "score", "weather in", "opening times",
             "what is", "what's the", "when did", "when is", "how do i", "how to", "define ", "meaning of")),
)


def keyword_plan(prompt: str) -> Plan:
    lowered = prompt.casefold()
    for route, words in KEYWORDS:
        if any(word in lowered for word in words):
            return Plan(route, prompt.strip())
    return Plan("chat", "")


# ---------------------------------------------------------------------------- "I don't know" → search online
SEARCH_REQUEST = re.compile(r"^\s*SEARCH\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
UNSURE = re.compile(
    r"\bI (?:do not|don't|really don't) (?:know|have (?:any |enough |specific |current |up-to-date |real-time |"
    r"recent |reliable )*(?:information|details|data|knowledge|access))|"
    r"\bI(?:'m| am) (?:not (?:sure|certain|aware)|unable to (?:find|browse|access|check|look)|unsure)|"
    r"\bI (?:can(?:no|')t|could(?:n't| not)) (?:find|browse|access|check|look up|confirm|verify|be sure)|"
    r"\b(?:my|the) (?:training )?(?:data|knowledge)(?: cut-?off| only goes)|\bknowledge cut-?off\b|"
    r"\b(?:no|not have) (?:access to )?(?:the internet|real-time|live|current) (?:data|information|access|updates)|"
    r"\bI (?:have no|haven't got any) (?:information|details|record)|"
    r"\b(?:recommend|suggest) (?:checking|searching|looking (?:it )?up)|"
    r"\b(?:check|search|look it up) (?:online|on the (?:web|internet))|"
    r"\b(?:the )?(?:source data|notes?|data (?:provided|given)) (?:do(?:es)?n't|do(?:es)? not) "
    r"(?:mention|contain|include|say|answer)",
    re.IGNORECASE)
PERSONAL = re.compile(r"\b(my|our|mine|ours)\b", re.IGNORECASE)


def search_request(text: str) -> str | None:
    """The chat model answered 'SEARCH: <query>' → the query."""
    match = SEARCH_REQUEST.match(text)
    if not match:
        return None
    query = match.group(1).strip().splitlines()[0].strip().strip('"“”')
    return query[:200] or None


def looks_unsure(answer: str) -> bool:
    """Script check: did the model say it doesn't know / can't check? (only the opening and closing count)"""
    text = answer.strip()
    return bool(text) and bool(UNSURE.search(text[:400]) or UNSURE.search(text[-300:]))


def is_personal(prompt: str) -> bool:
    """About Chris's own life ('my car', 'our holiday') — never sent to a search engine."""
    return bool(PERSONAL.search(prompt))
