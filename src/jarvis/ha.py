"""Home Assistant REST API client, entity matching and command parsing (no LLM).

Only entities you can see in Home Assistant are reachable, and every action goes through
a confirmation step in Jarvis before it runs (now or at the scheduled time).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import httpx

from . import http

STOP = {"the", "a", "an", "my", "our", "in", "on", "of", "please", "all", "to", "and"}
# words in questions that also appear in entity names but say nothing about which device is meant
QUESTION_WORDS = {"what", "whats", "is", "are", "how", "home", "house", "at", "me", "tell", "current", "currently",
                  "now", "right", "today", "it", "there", "any", "much", "many", "state", "status", "value", "you",
                  "i", "do", "does", "can", "could", "please", "show", "give", "check", "for", "with", "from", "up",
                  "turn", "set", "switch", "off", "open", "close", "was", "be", "time", "day", "last", "next",
                  "read", "say", "says", "about", "out", "loud"}
NO_READING = {"unavailable", "unknown", "", "none"}
IGNORED_DOMAINS = {"update", "button", "event", "zone", "sun", "tts", "stt", "conversation", "person",
                   "device_tracker", "automation", "script", "scene", "input_button", "tag", "todo", "calendar"}
DOMAIN_HINTS = {
    "light": ("light", "lights", "lamp", "lamps", "bulb", "bulbs"),
    "climate": ("heating", "thermostat", "radiator", "ac", "air", "conditioning", "hvac"),
    "cover": ("blind", "blinds", "curtain", "curtains", "shutter", "shutters", "garage", "gate"),
    "lock": ("lock",),
    "media_player": ("tv", "television", "speaker", "speakers", "music", "sonos", "radio"),
    "fan": ("fan", "fans"),
    "switch": ("plug", "socket", "switch", "outlet"),
    "scene": ("scene",),
    "script": ("script",),
}
ON_OFF_DOMAINS = {"light", "switch", "fan", "input_boolean", "media_player", "climate", "automation", "humidifier",
                  "siren", "water_heater", "vacuum", "remote"}
# what each kind of command may act on — read-only domains (sensor, binary_sensor, weather, person…) never appear
CONTROLLABLE = {
    "on": ON_OFF_DOMAINS | {"scene", "script", "input_boolean"},
    "off": ON_OFF_DOMAINS | {"input_boolean"},
    "lock": {"lock"},
    "unlock": {"lock"},
    "open": {"cover", "valve"},
    "close": {"cover", "valve"},
    "activate": {"scene", "script", "automation"},
    "set": {"climate", "water_heater", "light", "cover", "valve", "fan", "number", "input_number", "humidifier",
            "media_player"},
}
READ_ONLY = {"sensor": "sensor", "binary_sensor": "sensor", "weather": "weather entity", "person": "person",
             "device_tracker": "tracker", "sun": "sun entity", "zone": "zone", "event": "event entity",
             "update": "update entity", "image": "image", "camera": "camera"}
BLOCKED = {("lock", "unlock"), ("alarm_control_panel", "alarm_disarm")}


ENTITY_ID = re.compile(r"\b((?:water_heater|climate|light|switch|sensor|binary_sensor|cover|fan|lock|media_player|"
                       r"input_boolean|input_number|input_select|number|select|scene|script|automation|humidifier|"
                       r"valve|vacuum|siren|remote|camera|alarm_control_panel|button|weather)\.[a-z0-9_]+)\b")
ALIAS_PATTERNS = [
    re.compile(r"^(?P<name>.+?)\s+(?:is|=|means|refers to|is called)\s+(?:the\s+|my\s+)?(?:home assistant\s+|ha\s+)?"
               r"(?:entity\s+|device\s+|sensor\s+)?(?P<id>{E})"
               r"(?:\s+in\s+(?:home assistant|ha))?$"),
    re.compile(r"^(?P<id>{E})\s+(?:is|=|means)\s+(?P<name>.+)$"),
    re.compile(r"^call\s+(?P<id>{E})\s+(?P<name>.+)$"),
]
ALIAS_PATTERNS = [re.compile(p.pattern.replace("{E}", ENTITY_ID.pattern[2:-2]), re.I) for p in ALIAS_PATTERNS]


def parse_alias(text: str) -> tuple[str, str] | None:
    """'the gas water heater is the entity water_heater.thermostat1' → ('gas water heater', 'water_heater.thermostat1')."""
    clean = re.sub(r"\s+", " ", text).strip(" .!?`'\"“”")
    for pattern in ALIAS_PATTERNS:
        match = pattern.match(clean)
        if match:
            name = re.sub(r"^(?:the|my|our|a|an)\s+", "", match.group("name").strip(" .,`'\"“”"), flags=re.I)
            name = re.sub(r"\s+(?:in home assistant|in ha)$", "", name, flags=re.I).casefold()
            if 2 <= len(name) <= 60 and not ENTITY_ID.search(name):
                return name, match.group("id").casefold()
    return None


class HAError(Exception):
    """A user-facing Home Assistant error."""


@dataclass
class Command:
    verb: str                  # on | off | lock | unlock | open | close | set | activate
    target: str
    value: float | None = None
    unit: str = ""


@dataclass
class Action:
    domain: str
    service: str
    entity_ids: list[str]
    names: list[str]
    data: dict = field(default_factory=dict)

    @property
    def description(self) -> str:
        who = ", ".join(self.names[:4]) + (f" +{len(self.names) - 4}" if len(self.names) > 4 else "")
        verb = {"turn_on": "Turn on", "turn_off": "Turn off", "lock": "Lock", "unlock": "Unlock",
                "open_cover": "Open", "close_cover": "Close", "set_temperature": "Set",
                "set_cover_position": "Set", "set_value": "Set"}.get(self.service, self.service.replace("_", " ").capitalize())
        extra = ""
        if "temperature" in self.data:
            extra = f" to {self.data['temperature']}°"
        elif "brightness_pct" in self.data:
            extra = f" to {self.data['brightness_pct']}%"
        elif "position" in self.data:
            extra = f" to {self.data['position']}%"
        elif "value" in self.data:
            extra = f" to {self.data['value']}"
        if self.domain in {"scene", "script"}:
            return f"Run {who}"
        return f"{verb} {who}{extra}"

    def payload(self) -> dict:
        return {"domain": self.domain, "service": self.service, "entity_ids": self.entity_ids,
                "names": self.names, "data": self.data}


COMMANDS = [
    ("onoff", re.compile(r"^(?:please\s+)?(?:turn|switch|put)\s+(on|off)\s+(.+)$", re.I)),
    ("onoff_tail", re.compile(r"^(?:please\s+)?(?:turn|switch|put)\s+(.+?)\s+(on|off)$", re.I)),
    ("lock", re.compile(r"^(?:please\s+)?(lock|unlock)\s+(.+)$", re.I)),
    ("cover", re.compile(r"^(?:please\s+)?(open|close|shut)\s+(.+)$", re.I)),
    ("set", re.compile(r"^(?:please\s+)?(?:set|turn)\s+(.+?)\s+(?:up\s+|down\s+)?to\s+(\d+(?:\.\d+)?)\s*(degrees?|°c?|%|percent)?$", re.I)),
    ("activate", re.compile(r"^(?:please\s+)?(?:activate|run|start)\s+(.+)$", re.I)),
]


def parse_command(text: str) -> Command | None:
    text = re.sub(r"\s+", " ", text).strip(" .!?")
    for kind, pattern in COMMANDS:
        match = pattern.match(text)
        if not match:
            continue
        if kind == "onoff":
            return Command(match.group(1).casefold(), match.group(2))
        if kind == "onoff_tail":
            return Command(match.group(2).casefold(), match.group(1))
        if kind == "lock":
            return Command(match.group(1).casefold(), match.group(2))
        if kind == "cover":
            return Command("close" if match.group(1).casefold() in {"close", "shut"} else "open", match.group(2))
        if kind == "set":
            unit = (match.group(3) or "").casefold()
            return Command("set", match.group(1), float(match.group(2)), "%" if unit in {"%", "percent"} else unit)
        if kind == "activate":
            return Command("activate", match.group(1))
    return None


def tokens(text: str) -> set[str]:
    words = {w for w in re.findall(r"[a-z0-9]+", text.casefold().replace("_", " ")) if w not in STOP}
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


class HomeAssistant:
    def __init__(self, url: str, token: str, verify_tls: bool = True) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.verify = verify_tls
        self._states: tuple[float, list[dict]] = (0.0, [])
        self.alias_source = None  # async () -> {name: entity_id}; names Chris taught Jarvis ("gas water heater")
        self._alias_cache: tuple[str, dict[str, str]] | None = None  # (trace id, names)
        self._name_table_for: list[dict] | None = None   # the states list the table below was built from
        self._name_table_cache: tuple[list[tuple[str, str, set[str]]], dict[str, int]] | None = None
        self.last_alias_problem: tuple[str, str, list[str]] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def _request(self, method: str, path: str, body: dict | None = None) -> object:
        if not self.configured:
            raise HAError("Home Assistant is not configured (HA_URL / HA_TOKEN).")
        try:
            client = http.shared(timeout=20, verify=self.verify)
            response = await client.request(method, self.url + path, json=body,
                                            headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as error:
            raise HAError(f"Home Assistant is not reachable ({type(error).__name__}).") from None
        if response.status_code == 401:
            raise HAError("Home Assistant rejected the token (HA_TOKEN).")
        if response.status_code >= 400:
            raise HAError(f"Home Assistant returned HTTP {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    async def health(self) -> dict:
        if not self.configured:
            return {"ok": False, "detail": "not configured (HA_URL / HA_TOKEN)"}
        try:
            await self._request("GET", "/api/")
        except HAError as error:
            return {"ok": False, "detail": str(error)}
        return {"ok": True, "detail": f"ready ({self.url})"}

    async def states(self, max_age: float = 30) -> list[dict]:
        cached_at, states = self._states
        if time.time() - cached_at < max_age and states:
            return states
        result = await self._request("GET", "/api/states")
        states = result if isinstance(result, list) else []
        self._states = (time.time(), states)
        return states

    async def call(self, domain: str, service: str, entity_ids: list[str], data: dict | None = None) -> object:
        if (domain, service) in BLOCKED:
            raise HAError("For safety, Jarvis won't do that. Use the Home Assistant app.")
        body = {"entity_id": entity_ids if len(entity_ids) > 1 else entity_ids[0], **(data or {})}
        self._states = (0.0, [])
        return await self._request("POST", f"/api/services/{domain}/{service}", body)

    # to-do lists (the shopping list) ---------------------------------------------
    async def todo_items(self, entity_id: str) -> list[str]:
        """Items still to get on a Home Assistant to-do list."""
        result = await self._request("POST", "/api/services/todo/get_items?return_response",
                                     {"entity_id": entity_id, "status": "needs_action"})
        response = (result or {}).get("service_response", {}) if isinstance(result, dict) else {}
        items = (response.get(entity_id) or {}).get("items", []) or []
        return [str(i.get("summary", "")) for i in items if i.get("summary")]

    async def todo_add(self, entity_id: str, item: str) -> None:
        await self._request("POST", "/api/services/todo/add_item", {"entity_id": entity_id, "item": item[:200]})

    async def todo_complete(self, entity_id: str, item: str) -> bool:
        wanted = item.strip().casefold()
        for name in await self.todo_items(entity_id):
            if name.casefold() == wanted or wanted in name.casefold():
                await self._request("POST", "/api/services/todo/update_item",
                                    {"entity_id": entity_id, "item": name, "status": "completed"})
                return True
        return False

    # matching -----------------------------------------------------------------
    @staticmethod
    def name_of(state: dict) -> str:
        return (state.get("attributes") or {}).get("friendly_name") or state.get("entity_id", "")

    async def aliases(self) -> dict[str, str]:
        """Taught names → entity ids. One chat turn or job run asks for these several times (routing, gathering,
        resolving), and the source reads a vault note each time — so the answer is cached per trace. A new turn
        always reads afresh, so a name taught or typed into Jarvis/Home names.md counts straight away."""
        if self.alias_source is None:
            return {}
        from . import diag
        trace_id = diag.current_trace_id()
        cached = self._alias_cache
        if trace_id and cached is not None and cached[0] == trace_id:
            return cached[1]
        try:
            names = await self.alias_source()
        except Exception as error:  # noqa: BLE001 — matching still works without them
            diag.warning("home", f"couldn't read your taught home names: {type(error).__name__}: {error}")
            return {}
        if trace_id:
            self._alias_cache = (trace_id, names)
        return names

    def forget_aliases(self) -> None:
        self._alias_cache = None

    def find_entity(self, entity_id: str, states: dict[str, dict]) -> tuple[str | None, list[str]]:
        """Resolve an entity ID as typed. Exact match, else one that differs only in _ . - (water_heater.thermostat1
        → water_heater.thermostat_1). Returns (entity_id or None, close suggestions)."""
        if entity_id in states:
            return entity_id, []
        squash = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())  # noqa: E731
        same = [e for e in states if squash(e) == squash(entity_id)]
        if len(same) == 1:
            return same[0], []
        import difflib
        domain = entity_id.split(".", 1)[0]
        pool = [e for e in states if e.startswith(domain + ".")] or list(states)
        return None, same or difflib.get_close_matches(entity_id, pool, n=3, cutoff=0.6)

    async def alias_matches(self, text: str, domains: set[str] | None = None) -> list[dict]:
        """Entities whose taught name appears in `text` — longest name wins ("gas water heater" beats "heater")."""
        from . import diag
        wanted = tokens(text)
        states = {s.get("entity_id"): s for s in await self.states()}
        names = await self.aliases()
        best: list[tuple[int, dict]] = []
        for alias, entity_id in names.items():
            words = tokens(alias)
            if not words or not words <= wanted:
                continue
            found, suggestions = self.find_entity(entity_id, states)
            if found is None:
                diag.warning("home", f"“{alias}” is linked to {entity_id}, but Home Assistant has no such entity",
                             did_you_mean=suggestions)
                self.last_alias_problem = (alias, entity_id, suggestions)
                continue
            if found != entity_id:
                diag.debug("home", f"“{alias}”: using {found} for {entity_id}")
            if domains and found.split(".", 1)[0] not in domains:
                continue
            best.append((len(words), states[found]))
        if not best:
            diag.debug("home", f"no taught name in “{text[:60]}”", taught_names=len(names))
            return []
        top = max(n for n, _ in best)
        return [state for n, state in best if n == top]

    async def match_scored(self, target: str, domains: set[str] | None = None) -> tuple[float, list[dict]]:
        taught = await self.alias_matches(target, domains)
        if taught:  # a name Chris taught Jarvis beats any guess from entity names
            from . import diag
            diag.debug("home", f"“{target}” is a name you taught me", entities=[s.get("entity_id") for s in taught])
            return 5.0, taught
        wanted = tokens(target)
        hint_words = {d: tokens(" ".join(words)) for d, words in DOMAIN_HINTS.items()}
        spoken = {d for d, words in hint_words.items() if wanted & words}  # "lights" → light
        hinted = ((spoken & domains) or set(domains)) if domains else spoken
        # drop the hint words ("lights") from the name only when they actually pointed at a domain we'll use
        used = spoken & hinted
        name_tokens = wanted - set().union(*(hint_words[d] for d in used)) if used else wanted
        scored: list[tuple[float, dict]] = []
        for state in await self.states():
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".", 1)[0]
            if hinted and domain not in hinted:
                continue
            if domains and domain not in domains:
                continue
            have = tokens(self.name_of(state)) | tokens(entity_id.split(".", 1)[-1])
            basis = name_tokens or wanted
            if not basis:
                continue
            score = len(basis & have) / len(basis)
            if score >= 0.5:
                scored.append((score + (0.01 if domain in hinted else 0), state))
        from . import diag  # local import keeps ha.py usable standalone
        diag.debug("home", f"matching “{target}”", wanted=sorted(wanted), domains=sorted(hinted or domains or []),
                   candidates=sorted(((round(sc, 2), st.get("entity_id")) for sc, st in scored), reverse=True)[:15])
        if not scored:
            return 0.0, []
        best = max(score for score, _ in scored)
        return best, [state for score, state in scored if score >= best - 1e-9]

    async def last_known(self, entity_id: str, days: int = 7) -> dict | None:
        """The most recent real reading (not unavailable/unknown) from Home Assistant's history, or None."""
        from datetime import datetime, timedelta, timezone
        from urllib.parse import quote
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        try:
            result = await self._request(
                "GET", f"/api/history/period/{quote(start)}?filter_entity_id={quote(entity_id)}"
                       "&minimal_response&no_attributes")
        except HAError:
            return None
        rows = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else []
        for row in reversed(rows):
            if isinstance(row, dict) and str(row.get("state", "")).casefold() not in NO_READING:
                return {"state": row.get("state"), "at": row.get("last_changed") or row.get("last_updated") or ""}
        return None

    def _name_table(self, states: list[dict]) -> tuple[list[tuple[str, str, set[str]]], dict[str, int]]:
        """Per-entity word sets and how common each word is, built once per states snapshot (the snapshot is
        cached for minutes; this used to be recomputed for every chat message)."""
        if self._name_table_for is states and self._name_table_cache is not None:
            return self._name_table_cache
        names: list[tuple[str, str, set[str]]] = []
        frequency: dict[str, int] = {}
        for state in states:
            entity_id = state.get("entity_id", "")
            if entity_id.split(".", 1)[0] in IGNORED_DOMAINS:
                continue
            have = tokens(self.name_of(state)) | tokens(entity_id.split(".", 1)[-1])
            names.append((entity_id, self.name_of(state), have))
            for word in have:
                frequency[word] = frequency.get(word, 0) + 1
        self._name_table_for, self._name_table_cache = states, (names, frequency)
        return names, frequency

    async def mentioned(self, text: str, timeout: float = 3.0) -> list[tuple[float, str, str]]:
        """Entities a question seems to be about, found by script: (score, entity_id, name), best first.

        Uses how rare each word is across all entity names, so "cylinder" (one sensor) counts and "temperature"
        (dozens) barely does. Returns [] if Home Assistant is slow or down — routing must never wait on it."""
        if not self.configured:
            return []
        try:
            states = await asyncio.wait_for(self.states(max_age=300), timeout)
        except (HAError, asyncio.TimeoutError):
            return []
        wanted = tokens(text) - QUESTION_WORDS
        if not wanted:
            return []
        names, frequency = self._name_table(states)
        found = []
        for entity_id, name, have in names:
            hits = {w for w in wanted & have if len(w) >= 3 and not w.isdigit()}  # "1" or "tv2" alone mean nothing
            # at least one fairly distinctive word must match ("cylinder", "garage", "kitchen"), not just "sensor"
            if not hits or min(frequency[w] for w in hits) > max(5, len(names) // 20):
                continue
            # …and the question must cover a fair part of the name: two words, or at least half of it
            # ("garage" fits "Garage Door", but "read" doesn't make "Meter Last Read" the subject)
            named = {w for w in have if len(w) >= 3 and not w.isdigit()}
            if len(hits) < 2 and len(hits) * 2 < len(named):
                continue
            score = sum(1 / frequency[w] for w in hits) + len(hits)
            found.append((round(score, 3), entity_id, name))
        for state in await self.alias_matches(text):
            found.append((10.0, state.get("entity_id", ""), self.name_of(state)))
        found.sort(reverse=True)
        return found[:10]

    async def resolve(self, command: Command) -> Action:
        verb = command.verb
        domains = CONTROLLABLE.get(verb)
        self.last_alias_problem = None
        typed = ENTITY_ID.search(command.target.casefold())
        if typed:  # "turn on water_heater.thermostat_1" — an entity ID given directly
            states = {s.get("entity_id"): s for s in await self.states()}
            found, suggestions = self.find_entity(typed.group(1), states)
            if found is None:
                hint = f" Did you mean {' or '.join(f'`{x}`' for x in suggestions)}?" if suggestions else ""
                raise HAError(f"Home Assistant has no entity `{typed.group(1)}`.{hint}")
            domain = found.split(".", 1)[0]
            if domains and domain not in domains:
                raise HAError(f"`{found}` can't be {'set' if verb == 'set' else 'turned ' + verb if verb in {'on', 'off'} else verb + 'ed'}"
                              f" — {READ_ONLY.get(domain, domain)} entities " + ("can only be read." if domain in READ_ONLY else "don't support that."))
            return self._action(verb, domain, [found], [self.name_of(states[found])], command)
        score, matches = await self.match_scored(command.target, domains)
        # the name may fit a read-only entity better ("hall motion" is a motion sensor, not the hall light)
        any_score, any_matches = await self.match_scored(command.target)
        readable = [m for m in any_matches if m["entity_id"].split(".", 1)[0] in READ_ONLY]
        if readable and any_score > score + 0.02:
            kind = READ_ONLY[readable[0]["entity_id"].split(".", 1)[0]]
            raise HAError(f"“{self.name_of(readable[0])}” is a {kind} in Home Assistant — it can only be read, "
                          f"not controlled. Ask me what it says instead.")
        if not matches and self.last_alias_problem:
            alias, entity_id, suggestions = self.last_alias_problem
            hint = f" Did you mean {' or '.join(f'`{x}`' for x in suggestions)}?" if suggestions else ""
            raise HAError(f"You told me “{alias}” is `{entity_id}`, but Home Assistant has no entity with that ID."
                          f"{hint} Teach me again with “remember the {alias} is <entity id>”.")
        if not matches:
            raise HAError(f"I couldn't find anything in Home Assistant called “{command.target}” that I can "
                          f"{'set' if verb == 'set' else 'turn ' + verb if verb in {'on', 'off'} else verb}.")
        domain = matches[0]["entity_id"].split(".", 1)[0]
        matches = [m for m in matches if m["entity_id"].startswith(domain + ".")]
        ids = [m["entity_id"] for m in matches]
        names = [self.name_of(m) for m in matches]
        return self._action(verb, domain, ids, names, command)

    @staticmethod
    def _action(verb: str, domain: str, ids: list[str], names: list[str], command: Command) -> Action:
        if verb in {"on", "off"}:
            service = f"turn_{verb}"
            action_domain = domain if domain in ON_OFF_DOMAINS | {"scene", "script"} else "homeassistant"
            if domain in {"scene", "script"} and verb == "off":
                raise HAError("Scenes and scripts can only be run, not turned off.")
            return Action(action_domain, service, ids, names)
        if verb in {"lock", "unlock"}:
            if verb == "unlock":
                raise HAError("For safety, Jarvis won't unlock doors. Use the Home Assistant app.")
            return Action("lock", "lock", ids, names)
        if verb in {"open", "close"}:
            return Action(domain, f"{verb}_{domain}", ids, names)  # open_cover / close_valve
        if verb == "activate":
            return Action(domain if domain in {"scene", "script"} else "automation",
                          "turn_on" if domain in {"scene", "script"} else "trigger", ids, names)
        value = command.value
        if domain in {"climate", "water_heater"}:
            return Action(domain, "set_temperature", ids, names, {"temperature": value})
        if domain == "humidifier":
            return Action("humidifier", "set_humidity", ids, names, {"humidity": int(max(0, min(100, value)))})
        if domain == "media_player":
            return Action("media_player", "volume_set", ids, names, {"volume_level": round(max(0, min(100, value)) / 100, 2)})
        if domain == "valve":
            return Action("valve", "set_valve_position", ids, names, {"position": int(max(0, min(100, value)))})
        if domain == "light":
            return Action("light", "turn_on", ids, names, {"brightness_pct": int(max(0, min(100, value)))})
        if domain == "cover":
            return Action("cover", "set_cover_position", ids, names, {"position": int(max(0, min(100, value)))})
        if domain == "fan":
            return Action("fan", "set_percentage", ids, names, {"percentage": int(max(0, min(100, value)))})
        return Action(domain, "set_value", ids, names, {"value": value})
