"""Diagnostics: structured logs grouped into traces, stored in SQLite and browsable in the UI.

- Every chat turn, background job run and API request runs inside a *trace*; everything logged
  while it runs (decisions, HTTP calls, model calls, errors) carries the trace id.
- All outgoing HTTP (Ollama, Obsidian, Google, Home Assistant, ntfy, Kokoro, weather) is recorded
  automatically with status and timing. Secrets are redacted; request/response bodies are only
  kept in verbose (debug) mode, truncated.
- `event()` records a decision with structured data, e.g. why an email was skipped.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import secrets
import sys
import time
import traceback
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .db import Database

DEBUG, INFO, WARNING, ERROR = 10, 20, 30, 40
LEVEL_NAMES = {DEBUG: "debug", INFO: "info", WARNING: "warning", ERROR: "error"}
SECRET_KEYS = {"password", "refresh_token", "access_token", "id_token", "client_secret", "code", "token", "api_key",
               "apikey", "key", "xi-api-key", "authorization", "secret", "totp"}
BODY_LIMIT = 4000

_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("jarvis_trace", default=None)
_expect_errors: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_expect_errors", default=False)
_store: "DiagStore | None" = None
log = logging.getLogger("jarvis.diag")


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {k: ("***" if str(k).casefold() in SECRET_KEYS else redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:200]]
    if isinstance(value, str) and len(value) > BODY_LIMIT:
        return value[:BODY_LIMIT] + f"…(+{len(value) - BODY_LIMIT} chars)"
    return value


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = urlencode([(k, "***" if k.casefold() in SECRET_KEYS else v) for k, v in parse_qsl(parts.query, True)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _body(content: bytes, content_type: str) -> Any:
    if not content:
        return None
    content_type = content_type.casefold()
    if "json" in content_type or content[:1] in (b"{", b"["):
        try:
            return redact(json.loads(content))
        except ValueError:
            pass
    if "x-www-form-urlencoded" in content_type:
        return redact(dict(parse_qsl(content.decode("utf-8", "replace"))))
    if content_type.startswith(("text/", "application/x-ndjson")) or "xml" in content_type or not content_type:
        text = content.decode("utf-8", "replace")
        return text[:BODY_LIMIT] + (f"…(+{len(text) - BODY_LIMIT} chars)" if len(text) > BODY_LIMIT else "")
    return f"<{len(content)} bytes {content_type}>"


class DiagStore:
    def __init__(self, db: Database, base_level: int = INFO, retention_days: int = 7, max_rows: int = 100_000) -> None:
        self.db = db
        self.base_level = base_level
        self.retention_days = retention_days
        self.max_rows = max_rows
        self.services: dict[str, str] = {}   # url prefix → service name
        self.service_map = None              # optional callable returning live {url prefix: name}
        self._writing = False

    # level control ---------------------------------------------------------------
    @property
    def verbose_until(self) -> float:
        return float(self.db.get("diag.verbose_until", 0) or 0)

    def set_verbose(self, minutes: int) -> float:
        until = time.time() + minutes * 60 if minutes > 0 else 0
        self.db.set("diag.verbose_until", until)
        return until

    @property
    def level(self) -> int:
        return DEBUG if time.time() < self.verbose_until else self.base_level

    def enabled(self, level: int) -> bool:
        return level >= self.level

    # writing -----------------------------------------------------------------------
    def write(self, level: int, source: str, message: str, data: Any = None, error: str = "",
              duration_ms: float | None = None, trace_id: str | None = None) -> None:
        try:
            if self._writing or not self.enabled(level):
                return
        except Exception:  # e.g. the database is closed during shutdown — logging must never break the app
            return
        trace = _trace.get()
        trace_id = trace_id or (trace["id"] if trace else "")
        self._writing = True
        try:
            if trace and not trace.get("persisted"):
                self._persist(trace)
            if isinstance(data, dict):
                data = {k: v for k, v in data.items() if v is not None} or None
            payload = json.dumps(redact(data), ensure_ascii=False, default=str)[:60_000] if data is not None else ""
            self.db.execute(
                "INSERT INTO logs (ts, level, source, trace, message, data, error, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), level, source[:60], trace_id, str(message)[:2000], payload, error[:20_000], duration_ms))
            if trace and level >= WARNING:
                trace["issues"] = trace.get("issues", 0) + 1
        except Exception as exc:  # never let logging break the app
            print(f"diag write failed: {exc}", file=sys.stderr)
        finally:
            self._writing = False

    def _persist(self, trace: dict) -> None:
        trace["persisted"] = True
        self.db.execute("INSERT OR IGNORE INTO traces (id, ts, kind, name, status) VALUES (?, ?, ?, ?, 'running')",
                        (trace["id"], trace["started"], trace["kind"], trace["name"][:200]))

    def service_for(self, url: str) -> str:
        mapping = dict(self.services)
        if self.service_map is not None:
            try:
                mapping.update(self.service_map())
            except Exception:
                pass
        for prefix, name in mapping.items():
            if prefix and url.startswith(prefix):
                return name
        host = urlsplit(url).hostname or ""
        if host.endswith("googleapis.com") or host.endswith("google.com"):
            return "google"
        if "open-meteo" in host:
            return "weather"
        if "elevenlabs" in host:
            return "elevenlabs"
        return host or "http"

    def prune(self) -> dict:
        cutoff = time.time() - self.retention_days * 86400
        self.db.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM traces WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (self.max_rows,))
        remaining = self.db.one("SELECT COUNT(*) n FROM logs")["n"]
        return {"rows": remaining}


# ------------------------------------------------------------------------------ module API
def install(store: DiagStore) -> None:
    """Route Python logging and httpx traffic into the store (idempotent)."""
    global _store
    _store = store
    root = logging.getLogger("jarvis")
    if not any(isinstance(h, _DBHandler) for h in root.handlers):
        root.addHandler(_DBHandler())
    root.setLevel(logging.DEBUG)
    _patch_httpx()


def store() -> DiagStore | None:
    return _store


def current_trace_id() -> str:
    trace = _trace.get()
    return trace["id"] if trace else ""


def event(source: str, message: str, /, level: int = INFO, **data: Any) -> None:
    if _store is not None:
        _store.write(level, source, message, data or None)


def debug(source: str, message: str, /, **data: Any) -> None:
    event(source, message, DEBUG, **data)


def warning(source: str, message: str, /, **data: Any) -> None:
    event(source, message, WARNING, **data)


def error(source: str, message: str, exc: BaseException | None = None, /, **data: Any) -> None:
    if _store is not None:
        text = "".join(traceback.format_exception(exc)) if exc is not None else ""
        _store.write(ERROR, source, message, data or None, error=text)


def verbose() -> bool:
    return _store is not None and _store.enabled(DEBUG)


@contextlib.contextmanager
def expected_errors() -> Iterator[None]:
    """HTTP 4xx inside this block are an expected fallback path; log them at debug level only."""
    token = _expect_errors.set(True)
    try:
        yield
    finally:
        _expect_errors.reset(token)


@contextlib.contextmanager
def trace(kind: str, name: str, always: bool = True, **data: Any) -> Iterator[dict]:
    """Group everything logged inside into one trace (a chat turn, a job run, a request).

    With always=False the trace is only stored if something is logged inside it or it sets a
    result — so quiet background ticks don't flood the log.
    """
    parent = _trace.get()
    if parent is not None:  # nested: stay in the parent trace, but mark the step
        started = time.perf_counter()
        event(kind, f"{name} started", DEBUG, **data)
        try:
            yield parent
        finally:
            event(kind, f"{name} finished", DEBUG, duration_ms=round((time.perf_counter() - started) * 1000, 1))
        return
    info = {"id": secrets.token_hex(6), "kind": kind, "name": name, "issues": 0, "started": time.time(),
            "persisted": False, "result": None}
    token = _trace.set(info)
    status = "ok"
    if _store is not None and always:
        _store._persist(info)
    if data:
        event(kind, f"{name} started", DEBUG, **data)
    try:
        yield info
    except BaseException as exc:
        status = "error"
        error(kind, f"{name} failed: {type(exc).__name__}: {exc}", exc)
        raise
    finally:
        duration = round((time.time() - info["started"]) * 1000, 1)
        if status == "ok" and info.get("issues"):
            status = "warning"
        if _store is not None:
            if info.get("result") is not None:
                _store.write(INFO if status == "ok" else WARNING, kind, f"{name} → {str(info['result'])[:300]}",
                             duration_ms=duration)
            if info.get("persisted"):
                _store.db.execute("UPDATE traces SET ended = ?, duration_ms = ?, status = ?, issues = ? WHERE id = ?",
                                  (time.time(), duration, status, info.get("issues", 0), info["id"]))
        _trace.reset(token)


class _DBHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if _store is None or record.name == "jarvis.diag":
            return
        level = DEBUG if record.levelno < INFO else INFO if record.levelno < WARNING else \
            WARNING if record.levelno < ERROR else ERROR
        text = ""
        if record.exc_info:
            text = "".join(traceback.format_exception(*record.exc_info))
        source = record.name.removeprefix("jarvis.")
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        _store.write(level, source, message, error=text)


_patched = False


def _patch_httpx() -> None:
    global _patched
    if _patched:
        return
    _patched = True
    original = httpx.AsyncClient.send

    async def send(self, request: httpx.Request, *args, **kwargs):
        if _store is None:
            return await original(self, request, *args, **kwargs)
        started = time.perf_counter()
        url = str(request.url)
        service = _store.service_for(url)
        detail: dict[str, Any] = {"method": request.method, "url": redact_url(url)}
        is_debug = _store.enabled(DEBUG)
        if is_debug:
            try:
                detail["request_body"] = _body(request.content, request.headers.get("content-type", ""))
            except httpx.RequestNotRead:
                detail["request_body"] = "<streamed>"
        try:
            response = await original(self, request, *args, **kwargs)
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            _store.write(WARNING, service, f"{request.method} {urlsplit(url).path} failed: {type(exc).__name__}",
                         detail | {"error": str(exc)}, duration_ms=elapsed)
            raise
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        detail["status"] = response.status_code
        level = WARNING if response.status_code >= 400 else (INFO if elapsed > 5000 else DEBUG)
        if level == WARNING and response.status_code < 500 and _expect_errors.get():
            level = DEBUG
        if (is_debug or response.status_code >= 400) and not kwargs.get("stream"):
            try:
                detail["response_body"] = _body(response.content, response.headers.get("content-type", ""))
            except (httpx.ResponseNotRead, httpx.StreamError):
                detail["response_body"] = "<streamed>"
        if level >= _store.level:
            _store.write(level, service, f"{request.method} {urlsplit(url).path} → {response.status_code}",
                         detail, duration_ms=elapsed)
        return response

    httpx.AsyncClient.send = send  # type: ignore[method-assign]


__all__ = ["DiagStore", "install", "trace", "event", "debug", "warning", "error", "verbose", "current_trace_id",
           "redact", "redact_url", "LEVEL_NAMES", "DEBUG", "INFO", "WARNING", "ERROR"]
