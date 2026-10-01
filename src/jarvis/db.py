"""SQLite storage for Jarvis state. The vault stays the source of truth for knowledge;
this database holds cursors, ingested source data, the vault write outbox, sessions and logs."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_counter INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    ts REAL NOT NULL,
    from_addr TEXT NOT NULL,
    from_name TEXT NOT NULL,
    to_addrs TEXT NOT NULL,
    subject TEXT NOT NULL,
    labels TEXT NOT NULL,
    bulk INTEGER NOT NULL,
    outgoing INTEGER NOT NULL,
    snippet TEXT NOT NULL,
    body TEXT NOT NULL,
    attachments TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS emails_from ON emails(from_addr);
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    subject TEXT NOT NULL,
    first_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL,
    path TEXT NOT NULL,
    summary TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    all_day INTEGER NOT NULL,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    attendees TEXT NOT NULL,
    status TEXT NOT NULL,
    updated TEXT NOT NULL,
    html_link TEXT NOT NULL,
    reminded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS people (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (day, kind, ref)
);
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    queued REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS note_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    actor TEXT NOT NULL,
    before TEXT,
    after TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    dedupe TEXT UNIQUE,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    priority INTEGER NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS event_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    source TEXT NOT NULL,              -- ics | jsonld | llm | chat
    fingerprint TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    email_subject TEXT NOT NULL DEFAULT '',
    ical_uid TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    start TEXT NOT NULL,               -- ISO datetime with offset, or YYYY-MM-DD for all-day
    end TEXT NOT NULL,
    all_day INTEGER NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    status TEXT NOT NULL,              -- pending | added | dismissed | duplicate | failed
    event_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    decided REAL
);
CREATE TABLE IF NOT EXISTS event_scan (
    message_id TEXT PRIMARY KEY,
    queued REAL NOT NULL,
    status TEXT NOT NULL,              -- pending | done | skipped
    body TEXT NOT NULL,
    subject TEXT NOT NULL,
    sender TEXT NOT NULL,
    ts REAL NOT NULL,
    thread_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_saves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    summary TEXT NOT NULL,
    created INTEGER NOT NULL,
    actor TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
    resource_name TEXT PRIMARY KEY,
    etag TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    data TEXT NOT NULL,                -- normalised JSON: emails, phones, birthday, relations, org, addresses...
    birthday TEXT NOT NULL DEFAULT ''  -- YYYY-MM-DD or --MM-DD when the year is unknown
);
CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    kind TEXT NOT NULL,                -- reminder | ha
    text TEXT NOT NULL,                -- what to remind / human description of the action
    due REAL,                          -- epoch seconds; NULL = run as soon as confirmed
    repeat TEXT NOT NULL DEFAULT '',   -- '' | daily | weekdays | weekly | monthly
    payload TEXT NOT NULL DEFAULT '{}',-- HA: {"domain","service","entity_id","data","name"}
    status TEXT NOT NULL,              -- proposed | scheduled | done | failed | cancelled | dismissed
    last_run REAL,
    result TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'chat'
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    updated REAL NOT NULL,             -- time of the latest status (email time or check time)
    retailer TEXT NOT NULL DEFAULT '',
    item TEXT NOT NULL DEFAULT '',
    carrier TEXT NOT NULL DEFAULT '',
    tracking_number TEXT NOT NULL DEFAULT '',
    tracking_url TEXT NOT NULL DEFAULT '',
    order_number TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ordered', -- ordered | dispatched | in_transit | out_for_delivery | attempted | delayed | delivered
    status_text TEXT NOT NULL DEFAULT '',
    expected TEXT NOT NULL DEFAULT '',     -- YYYY-MM-DD when known
    source TEXT NOT NULL DEFAULT 'email',  -- email | chat
    thread_id TEXT NOT NULL DEFAULT '',
    poll INTEGER NOT NULL DEFAULT 1,       -- follow the tracking link hourly
    poll_note TEXT NOT NULL DEFAULT '',
    check_failures INTEGER NOT NULL DEFAULT 0,
    last_checked REAL,
    active INTEGER NOT NULL DEFAULT 1,
    history TEXT NOT NULL DEFAULT '[]'      -- [{ts, status, text, via}]
);
CREATE INDEX IF NOT EXISTS deliveries_active ON deliveries(active, status);
CREATE TABLE IF NOT EXISTS ha_aliases (
    alias TEXT PRIMARY KEY,            -- lower-case name Chris uses ("gas water heater")
    entity_id TEXT NOT NULL,           -- water_heater.thermostat1
    source TEXT NOT NULL DEFAULT 'chat',
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS event_senders (
    sender TEXT PRIMARY KEY,           -- email address
    dismissed INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level INTEGER NOT NULL,            -- 10 debug, 20 info, 30 warning, 40 error
    source TEXT NOT NULL,
    trace TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    duration_ms REAL
);
CREATE INDEX IF NOT EXISTS events_start ON events(start);
CREATE INDEX IF NOT EXISTS event_proposals_start ON event_proposals(start);
CREATE INDEX IF NOT EXISTS event_proposals_status ON event_proposals(status);
CREATE INDEX IF NOT EXISTS scheduled_status_due ON scheduled(status, due);
CREATE INDEX IF NOT EXISTS notifications_status_ts ON notifications(status, ts);
CREATE INDEX IF NOT EXISTS logs_trace ON logs(trace);
CREATE INDEX IF NOT EXISTS logs_level ON logs(level, id);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                -- chat | job | request | client
    name TEXT NOT NULL,
    ended REAL,
    duration_ms REAL,
    status TEXT NOT NULL,              -- running | ok | warning | error
    issues INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS traces_ts ON traces(ts);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO auth (id) VALUES (1)")
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release."""
        additions = {"events": {"ical_uid": "TEXT NOT NULL DEFAULT ''"},
                     "chat_messages": {"trace": "TEXT NOT NULL DEFAULT ''"},
                     "event_proposals": {"sender": "TEXT NOT NULL DEFAULT ''",
                                         "calendar_id": "TEXT NOT NULL DEFAULT ''",
                                         "calendar_name": "TEXT NOT NULL DEFAULT ''",
                                         "kind": "TEXT NOT NULL DEFAULT 'add'",          # add | note
                                         "target_event_id": "TEXT NOT NULL DEFAULT ''"},  # note: event to update
                     "event_scan": {"automated": "INTEGER NOT NULL DEFAULT 0"},
                     "scheduled": {"decided": "REAL"},
                     "deliveries": {"item_checked": "INTEGER NOT NULL DEFAULT 0"}}
        for table, columns in additions.items():
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several statements into one commit. The connection runs in autocommit mode, so without this
        every statement is its own WAL commit (and fsync)."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    # key/value state -----------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # vault outbox --------------------------------------------------------
    def queue_note(self, kind: str, key: str) -> None:
        self.execute(
            "INSERT INTO outbox (kind, key, queued) VALUES (?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET queued = excluded.queued, attempts = 0, last_error = ''",
            (kind, key, time.time()),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
