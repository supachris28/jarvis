"""To-dos and the shopping list (Tier 0 — by script).

To-dos are things to do *sometime* (reminders are for a set time): "I need to sort the boiler service", "add renew
passport to my to-do list by Friday". They can have a due day, be snoozed and ticked off; today's and overdue ones
appear in the morning brief, and the list is mirrored to Jarvis/To do.md in the vault.

The shopping list lives in Home Assistant's to-do list (HA_SHOPPING_LIST, default todo.shopping_list) so it's on
your phone in the shop; without Home Assistant it's kept in Jarvis.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta

from .. import diag
from ..extract.event_text import _find_date
from ..ha import HAError
from ..vault.markdown import one_line

TASK_ADD = re.compile(
    r"^\s*(?:please\s+)?(?:(?:i|we)\s+(?:need|have|must|ought|should)\s+(?:to\s+)?|(?:i've|i have)\s+got\s+to\s+|"
    r"(?:add|put)\s+(?P<what>.+?)\s+(?:to|on)\s+(?:my|the|our)\s+(?:to-?do|todo|task)s?(?:\s+list)?\b|"
    r"(?:to-?do|todo|task)\s*:\s*)(?P<rest>.*)$", re.I | re.S)
SHOP_ADD = re.compile(
    r"^\s*(?:please\s+)?(?:add|put|we\s+need|i\s+need\s+to\s+buy|buy|get)\s+(?P<items>.+?)\s*"
    r"(?:(?:to|on)\s+(?:the|my|our)\s+(?:shopping|grocery|groceries)(?:\s+list)?)\s*[.!]?$|"
    r"^\s*(?:shopping|shopping list)\s*:\s*(?P<items2>.+)$", re.I | re.S)
LIST_QUESTION = re.compile(r"\b(?:what(?:'s| is)|show|read|list|tell me)\b.{0,25}\b(?P<which>to-?do|todo|tasks?|"
                           r"shopping)(?:\s+list)?\b|^\s*(?P<which2>to-?do|todo|tasks|shopping)(?:\s+list)?\s*\??\s*$",
                           re.I)
DONE = re.compile(r"^\s*(?:i(?:'ve| have)\s+)?(?:done|finished|completed|tick(?:ed)?\s+off|cross(?:ed)?\s+off|"
                  r"mark(?:ed)?\s+(?:as\s+)?done)\s*:?\s*(?P<what>.+?)(?:\s+(?:as\s+done|off|from\s+(?:my|the)\s+"
                  r"(?:to-?do|shopping)(?:\s+list)?))?\s*[.!]?$", re.I)
NOT_TASK = re.compile(r"\b(remind|reminder|calendar|diary)\b|\?\s*$|^\s*(?:i|we)\s+(?:need|have|must|should)\s+(?:to\s+)?"
                      r"(?:know|find out|ask (?:you|jarvis)|understand|see|tell|work out|check (?:if|whether|when|what))\b",
                      re.I)
DUE = re.compile(r"\s*\b(?:by|before|on|for|due)\s+(?P<when>.+?)\s*$", re.I)


def split_items(text: str) -> list[str]:
    """'milk, eggs and 2 loaves of bread' → ['milk', 'eggs', '2 loaves of bread']."""
    parts = re.split(r"\s*(?:,|;|\band\b|&|\+)\s*", text.strip(" .!"))
    return [p.strip(" .") for p in parts if p.strip(" .")][:20]


def parse_task(prompt: str, today: date) -> tuple[str, date | None] | None:
    """'I need to renew my passport by Friday' → ('Renew my passport', Fri)."""
    if NOT_TASK.search(prompt) or SHOP_ADD.match(prompt):
        return None
    match = TASK_ADD.match(prompt)
    if not match:
        return None
    text = (match.group("what") or match.group("rest") or "").strip(" .!")
    if len(text) < 3:
        return None
    due = None
    tail = DUE.search(text)
    if tail:
        found, rest = _find_date(tail.group("when"), today)
        if found is not None and not rest.strip(" ,."):
            due, text = found, text[:tail.start()].strip(" ,.")
    else:
        found, rest = _find_date(text, today, ("word", "weekday", "dmy", "num", "iso", "mdy"))
        if found is not None:
            due, text = found, re.sub(r"\s+", " ", rest).strip(" ,.")
    return (text[:1].upper() + text[1:])[:200], due


class Tasks:
    def __init__(self, settings, db, ha=None) -> None:
        self.settings = settings
        self.db = db
        self.ha = ha
        with db._lock:
            db._conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created REAL NOT NULL,
                    list TEXT NOT NULL DEFAULT 'todo',     -- todo | shopping (when Home Assistant isn't used)
                    text TEXT NOT NULL,
                    due TEXT NOT NULL DEFAULT '',           -- YYYY-MM-DD
                    snoozed TEXT NOT NULL DEFAULT '',       -- hidden until this day
                    done REAL,
                    source TEXT NOT NULL DEFAULT 'chat'
                );
            """)

    @property
    def today(self) -> date:
        return datetime.now(self.settings.tz).date()

    # ---------------------------------------------------------------- to-dos
    def add(self, text: str, due: date | None = None, source: str = "chat") -> dict:
        cursor = self.db.execute("INSERT INTO tasks (created, text, due, source) VALUES (?, ?, ?, ?)",
                                 (time.time(), text.strip()[:200], due.isoformat() if due else "", source))
        self.db.queue_note("tasks", "all")
        diag.event("tasks", f"added: {text[:80]}", due=due.isoformat() if due else "")
        return self.get(cursor.lastrowid)

    def get(self, task_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self.present(row) if row else None

    def present(self, row) -> dict:
        item = dict(row)
        today = self.today
        item["due_text"] = ""
        item["overdue"] = False
        if item["due"]:
            day = date.fromisoformat(item["due"])
            item["overdue"] = day < today and not item["done"]
            item["due_text"] = ("today" if day == today else "tomorrow" if day == today + timedelta(days=1)
                                else f"{day:%a} {day.day} {day:%b}")
        return item

    def open(self, include_snoozed: bool = False) -> list[dict]:
        rows = self.db.all("SELECT * FROM tasks WHERE list = 'todo' AND done IS NULL "
                           "ORDER BY due = '', due, id")
        today = self.today.isoformat()
        return [self.present(r) for r in rows if include_snoozed or not r["snoozed"] or r["snoozed"] <= today]

    def due_soon(self, days: int = 0) -> list[dict]:
        limit = (self.today + timedelta(days=days)).isoformat()
        return [t for t in self.open() if t["due"] and t["due"] <= limit]

    def find(self, words: str) -> dict | None:
        wanted = {w for w in re.findall(r"[a-z0-9]+", words.casefold()) if len(w) > 2}
        best, score = None, 0
        for task in self.open(include_snoozed=True):
            have = set(re.findall(r"[a-z0-9]+", task["text"].casefold()))
            hits = len(wanted & have)
            if hits > score:
                best, score = task, hits
        return best if best and score >= max(1, len(wanted) // 2) else None

    def complete(self, task_id: int, done: bool = True) -> dict | None:
        self.db.execute("UPDATE tasks SET done = ? WHERE id = ?", (time.time() if done else None, task_id))
        self.db.queue_note("tasks", "all")
        return self.get(task_id)

    def update(self, task_id: int, fields: dict) -> dict | None:
        allowed = {k: str(v)[:200] for k, v in fields.items() if k in ("text", "due", "snoozed")}
        if "due" in allowed and allowed["due"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", allowed["due"]):
            allowed.pop("due")
        if "snoozed" in allowed and allowed["snoozed"] in ("tomorrow", "week"):
            allowed["snoozed"] = (self.today + timedelta(days=1 if allowed["snoozed"] == "tomorrow" else 7)).isoformat()
        if allowed:
            self.db.execute(f"UPDATE tasks SET {', '.join(k + ' = ?' for k in allowed)} WHERE id = ?",
                            (*allowed.values(), task_id))
            self.db.queue_note("tasks", "all")
        return self.get(task_id)

    def delete(self, task_id: int) -> None:
        self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.db.queue_note("tasks", "all")

    def lines(self, tasks: list[dict]) -> list[str]:
        return [f"{'⚠️ ' if t['overdue'] else ''}{t['text']}" + (f" — {'was due ' if t['overdue'] else ''}{t['due_text']}"
                                                                  if t["due_text"] else "") for t in tasks]

    # ---------------------------------------------------------------- shopping
    @property
    def shopping_entity(self) -> str:
        return self.settings.ha_shopping_list or "todo.shopping_list"

    @property
    def shopping_in_ha(self) -> bool:
        return bool(self.ha is not None and self.ha.configured and self.settings.ha_shopping_list != "off")

    async def shopping(self) -> tuple[list[str], str]:
        """(items still needed, where the list lives)."""
        if self.shopping_in_ha:
            try:
                return await self.ha.todo_items(self.shopping_entity), "Home Assistant"
            except HAError as error:
                diag.warning("tasks", f"shopping list from Home Assistant: {error}")
                raise
        rows = self.db.all("SELECT text FROM tasks WHERE list = 'shopping' AND done IS NULL ORDER BY id")
        return [r["text"] for r in rows], "Jarvis"

    async def add_shopping(self, items: list[str]) -> str:
        if self.shopping_in_ha:
            for item in items:
                await self.ha.todo_add(self.shopping_entity, item)
            return "Home Assistant"
        for item in items:
            self.db.execute("INSERT INTO tasks (created, list, text) VALUES (?, 'shopping', ?)", (time.time(), item[:120]))
        return "Jarvis"

    async def tick_shopping(self, item: str) -> bool:
        if self.shopping_in_ha:
            return await self.ha.todo_complete(self.shopping_entity, item)
        row = self.db.one("SELECT id FROM tasks WHERE list = 'shopping' AND done IS NULL AND text = ? COLLATE NOCASE",
                          (item,))
        if row:
            self.db.execute("UPDATE tasks SET done = ? WHERE id = ?", (time.time(), row["id"]))
        return bool(row)

    # ---------------------------------------------------------------- chat
    async def handle(self, prompt: str) -> str | None:
        """A to-do or shopping request answered by script, or None when it isn't one."""
        shop = SHOP_ADD.match(prompt)
        if shop:
            items = split_items(shop.group("items") or shop.group("items2") or "")
            if not items:
                return None
            try:
                where = await self.add_shopping(items)
            except HAError as error:
                return f"I couldn't add that to the shopping list in Home Assistant: {error}"
            return f"Added to the shopping list ({where}): " + ", ".join(items) + "."
        question = LIST_QUESTION.search(prompt)
        if question:
            which = (question.group("which") or question.group("which2") or "").casefold()
            if which.startswith("shop"):
                try:
                    items, where = await self.shopping()
                except HAError as error:
                    return f"I couldn't read the shopping list from Home Assistant: {error}"
                return ("**Shopping list**\n" + "\n".join(f"- {i}" for i in items)) if items else \
                    "The shopping list is empty."
            tasks = self.open()
            return ("**To do**\n" + "\n".join(f"- {line}" for line in self.lines(tasks))) if tasks else \
                "Nothing on your to-do list."
        done = DONE.match(prompt)
        if done:
            task = self.find(done.group("what"))
            if task:
                self.complete(task["id"])
                return f"Ticked off: {task['text']}. ✅"
            if self.shopping_in_ha or self.db.one("SELECT 1 FROM tasks WHERE list = 'shopping' AND done IS NULL"):
                try:
                    if await self.tick_shopping(done.group("what").strip(" .!")):
                        return f"Ticked {done.group('what').strip(' .!')} off the shopping list."
                except HAError:
                    pass
            return None
        parsed = parse_task(prompt, self.today)
        if parsed:
            text, due = parsed
            task = self.add(text, due)
            return f"Added to your to-do list: **{task['text']}**" + (f" (due {task['due_text']})" if due else "") + "."
        return None
