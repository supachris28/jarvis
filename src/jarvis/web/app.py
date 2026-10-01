"""HTTP API and PWA for Jarvis (Starlette)."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__, diag, http
from ..auth import Auth
from ..config import Settings
from ..google.oauth import GoogleError
from ..services import Services
from ..tts import SpeechError
from ..ha import HAError
from ..vault.client import VaultError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "jarvis_session"
PUBLIC_PATHS = {"/", "/healthz", "/api/session", "/api/login", "/manifest.webmanifest", "/sw.js",
                "/auth/google/callback"}  # callback is protected by the OAuth state from /auth/google/start

QUIET_PATHS = {"/api/chat", "/api/activity", "/api/session", "/api/status", "/api/chat/history", "/api/tts"}

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; media-src 'self' blob: data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        auth: Auth = request.app.state.auth
        needs_auth = not (path in PUBLIC_PATHS or path.startswith("/static/"))
        if needs_auth and not auth.session_valid(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorised"}, status_code=401)
            return RedirectResponse("/")
        # CSRF: state-changing API calls must come from our own script (custom header + same origin).
        if request.method not in {"GET", "HEAD", "OPTIONS"} and path.startswith("/api/"):
            if request.headers.get("x-jarvis") != "1":
                return JSONResponse({"error": "missing request header"}, status_code=403)
            origin = request.headers.get("origin")
            expected = request.app.state.settings.public_url.rstrip("/")
            if origin and origin.rstrip("/") != expected and not expected.startswith("http://localhost"):
                return JSONResponse({"error": "bad origin"}, status_code=403)
        if path.startswith("/api/") and path not in QUIET_PATHS and not path.startswith("/api/diag"):
            with diag.trace("request", f"{request.method} {path}", always=False) as trace:
                try:
                    response = await call_next(request)
                except Exception as exc:
                    diag.error("web", f"{request.method} {path} crashed: {type(exc).__name__}: {exc}", exc)
                    response = JSONResponse({"error": f"Internal error ({type(exc).__name__}). See Logs.",
                                             "trace": trace["id"]}, status_code=500)
                if response.status_code >= 500:
                    diag.warning("web", f"{request.method} {path} → {response.status_code}")
                elif diag.verbose():
                    diag.debug("web", f"{request.method} {path} → {response.status_code}")
                if trace.get("persisted"):
                    response.headers["X-Jarvis-Trace"] = trace["id"]
        else:
            response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=()")
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


async def index(request: Request) -> Response:
    # versioned script/style URLs so a new release is never run with a stale app.js from the browser cache
    from .. import __version__
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for asset in ("/static/app.js", "/static/style.css"):
        html = html.replace(f'"{asset}"', f'"{asset}?v={__version__}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


async def manifest(request: Request) -> Response:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


async def service_worker(request: Request) -> Response:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


async def healthz(request: Request) -> Response:
    from .. import __version__
    return PlainTextResponse(f"ok {__version__}")  # the deploy script checks the new version is live


async def session(request: Request) -> Response:
    auth: Auth = request.app.state.auth
    return JSONResponse({
        "authenticated": auth.session_valid(request.cookies.get(COOKIE)),
        "password_set": auth.password_set,
        "totp": bool(auth.row["totp_enabled"]),
        "version": __version__,
        "vault": request.app.state.settings.obsidian_vault_name,
        "tts": request.app.state.services.speech.provider,
    })


async def login(request: Request) -> Response:
    auth: Auth = request.app.state.auth
    settings: Settings = request.app.state.settings
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if auth.throttled(client_ip(request)):
        return JSONResponse({"error": "Too many attempts. Wait 15 minutes."}, status_code=429)
    token = auth.login(client_ip(request), str(body.get("password", "")), str(body.get("code", "")))
    if not token:
        return JSONResponse({"error": "Incorrect password or code."}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, secure=settings.secure_cookies,
                        samesite="strict", path="/")
    return response


async def logout(request: Request) -> Response:
    request.app.state.auth.logout(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


async def chat(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad request"}, status_code=400)
    prompt = str(body.get("message", "")).strip()
    if not prompt:
        return JSONResponse({"error": "empty message"}, status_code=400)

    async def events():
        with diag.trace("chat", prompt[:120]) as trace:
            yield json.dumps({"type": "trace", "id": trace["id"]}) + "\n"
            diag.event("chat", "message received", chars=len(prompt), text=prompt[:500] if diag.verbose() else None)
            try:
                async for event in services.assistant.handle(prompt):
                    if event.get("type") == "meta":
                        diag.event("chat", f"route: {event.get('route')}", **{k: v for k, v in event.items() if k != "type"})
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except Exception as error:  # surface failures in the chat instead of a broken stream
                diag.error("chat", f"chat failed: {type(error).__name__}: {error}", error)
                yield json.dumps({"type": "token", "text": f"\n\n(Error: {type(error).__name__}: {error}. "
                                                           "Tap Details for the full log.)"}) + "\n"
                yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


async def chat_history(request: Request) -> Response:
    services: Services = request.app.state.services
    rows = services.db.all("SELECT id, ts, role, content, trace FROM chat_messages ORDER BY id DESC LIMIT 60")
    return JSONResponse([dict(r) for r in reversed(rows)])


async def activity(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        after = int(request.query_params.get("after", "0"))
    except ValueError:
        after = 0
    rows = services.db.all("SELECT id, ts, role, content FROM chat_messages WHERE role = 'activity' AND id > ? "
                           "ORDER BY id LIMIT 20", (after,))
    return JSONResponse([dict(r) for r in rows])


async def events_list(request: Request) -> Response:
    services: Services = request.app.state.services
    status_ = request.query_params.get("status", "pending")
    if status_ not in {"pending", "added", "dismissed", "failed", "all"}:
        status_ = "pending"
    return JSONResponse({"items": services.events.list(status_),
                         "can_add": services.oauth.has_scope("https://www.googleapis.com/auth/calendar.events"),
                         "queue": services.db.one("SELECT COUNT(*) n FROM event_scan WHERE status = 'pending'")["n"]})


async def event_add(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        overrides = await request.json()
    except ValueError:
        overrides = {}
    allowed = {k: overrides[k] for k in ("title", "start", "end", "location", "notes", "all_day") if k in overrides}
    try:
        item = await services.events.accept(int(request.path_params["id"]), allowed)
    except GoogleError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    services.trigger("calendar")
    return JSONResponse(item)


async def event_dismiss(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    mute = bool(body.get("mute")) if isinstance(body, dict) else False
    result = request.app.state.services.events.dismiss(int(request.path_params["id"]), mute=mute)
    return JSONResponse({"ok": True, **result})


async def event_senders(request: Request) -> Response:
    return JSONResponse({"muted": request.app.state.services.events.muted_senders()})


async def event_sender_unmute(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    sender = str(body.get("sender", "")).strip() if isinstance(body, dict) else ""
    if not sender:
        return JSONResponse({"error": "sender required"}, status_code=400)
    request.app.state.services.events.unmute(sender)
    return JSONResponse({"ok": True})


async def scheduled_list(request: Request) -> Response:
    services: Services = request.app.state.services
    return JSONResponse({"open": services.scheduler.list(), "recent": services.scheduler.recent(),
                         "history": services.history(), "home": services.ha.configured})


async def scheduled_confirm(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        item = await services.scheduler.confirm(int(request.path_params["id"]))
    except HAError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    services.trigger("vault")
    return JSONResponse(item)


async def scheduled_cancel(request: Request) -> Response:
    services: Services = request.app.state.services
    services.scheduler.cancel(int(request.path_params["id"]))
    services.trigger("vault")
    return JSONResponse({"ok": True})


async def brief_now(request: Request) -> Response:
    services: Services = request.app.state.services
    return JSONResponse({"text": await services.brief.build()})


# ---------------------------------------------------------------------------- diagnostics
def _log_row(row) -> dict:
    item = dict(row)
    item["level"] = diag.LEVEL_NAMES.get(item["level"], str(item["level"]))
    try:
        item["data"] = json.loads(item["data"]) if item["data"] else None
    except ValueError:
        pass
    return item


LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


async def diag_logs(request: Request) -> Response:
    services: Services = request.app.state.services
    q = request.query_params
    clauses, params = ["level >= ?"], [LEVELS.get(q.get("level", "info"), 20)]
    if q.get("trace"):
        clauses, params = ["trace = ?"], [q["trace"]]
    if q.get("source"):
        clauses.append("source = ?")
        params.append(q["source"])
    if q.get("q"):
        clauses.append("(message LIKE ? OR data LIKE ? OR error LIKE ?)")
        params += [f"%{q['q']}%"] * 3
    if q.get("before", "").isdigit():
        clauses.append("id < ?")
        params.append(int(q["before"]))
    limit = min(int(q["limit"]) if q.get("limit", "").isdigit() else 200, 1000)
    order = "ASC" if q.get("trace") else "DESC"
    rows = services.db.all(f"SELECT * FROM logs WHERE {' AND '.join(clauses)} ORDER BY id {order} LIMIT ?",
                           (*params, limit))
    items = [_log_row(r) for r in rows]
    trace = None
    if q.get("trace"):
        row = services.db.one("SELECT * FROM traces WHERE id = ?", (q["trace"],))
        trace = dict(row) if row else None
    return JSONResponse({"items": items, "trace": trace,
                         "next_before": items[-1]["id"] if items and order == "DESC" and len(items) == limit else None})


async def diag_traces(request: Request) -> Response:
    services: Services = request.app.state.services
    q = request.query_params
    clauses, params = ["1 = 1"], []
    if q.get("kind"):
        clauses.append("kind = ?")
        params.append(q["kind"])
    if q.get("problems") == "1":
        clauses.append("status IN ('warning', 'error')")
    rows = services.db.all(f"SELECT * FROM traces WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT 100", params)
    return JSONResponse([dict(r) for r in rows])


async def diag_meta(request: Request) -> Response:
    services: Services = request.app.state.services
    sources = [r["source"] for r in services.db.all(
        "SELECT DISTINCT source FROM logs WHERE ts > ? ORDER BY source", (time.time() - 7 * 86400,))]
    counts = {diag.LEVEL_NAMES[r["level"]]: r["n"] for r in services.db.all(
        "SELECT level, COUNT(*) n FROM logs WHERE ts > ? GROUP BY level", (time.time() - 86400,))
        if r["level"] in diag.LEVEL_NAMES}
    return JSONResponse({"sources": sources, "last_24h": counts, "verbose_until": services.diag.verbose_until,
                         "base_level": diag.LEVEL_NAMES[services.diag.base_level],
                         "retention_days": services.diag.retention_days})


async def diag_verbose(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        minutes = int((await request.json()).get("minutes", 60))
    except (ValueError, AttributeError):
        minutes = 60
    until = services.diag.set_verbose(max(0, min(minutes, 24 * 60)))
    diag.event("diag", f"verbose logging {'on until ' + time.strftime('%H:%M', time.localtime(until)) if until else 'off'}")
    return JSONResponse({"verbose_until": until})


async def diag_client(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    with diag.trace("client", "browser error"):
        diag.warning("browser", str(body.get("message", "error"))[:500],
                     stack=str(body.get("stack", ""))[:4000], page=str(body.get("url", ""))[:300],
                     agent=request.headers.get("user-agent", "")[:200])
    return JSONResponse({"ok": True})


async def diag_export(request: Request) -> Response:
    services: Services = request.app.state.services
    q = request.query_params
    if q.get("trace"):
        rows = services.db.all("SELECT * FROM logs WHERE trace = ? ORDER BY id", (q["trace"],))
        name = f"jarvis-trace-{q['trace']}.json"
    else:
        hours = min(int(q.get("hours", "24") or 24), 24 * 14)
        rows = services.db.all("SELECT * FROM logs WHERE ts > ? ORDER BY id", (time.time() - hours * 3600,))
        name = f"jarvis-logs-{time.strftime('%Y%m%d-%H%M')}.json"
    body = json.dumps({"version": __version__, "exported": time.time(), "logs": [_log_row(r) for r in rows]},
                      ensure_ascii=False, indent=1, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def tts(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str(body.get("text", ""))[:4000]
    if services.speech.provider == "browser":
        return JSONResponse({"provider": "browser"}, status_code=409)
    try:
        audio, media_type = await services.speech.synthesize(text)
    except SpeechError as error:
        return JSONResponse({"error": str(error), "provider": services.speech.provider}, status_code=502)
    return Response(audio, media_type=media_type, headers={"Cache-Control": "no-store"})


async def chat_clear(request: Request) -> Response:
    request.app.state.services.db.execute("DELETE FROM chat_messages")
    return JSONResponse({"ok": True})


async def notify_prefs(request: Request) -> Response:
    notifier = request.app.state.services.notifier
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return JSONResponse({"categories": notifier.set_preferences(body if isinstance(body, dict) else {}),
                             "quiet_hours": notifier.settings.notify_quiet_hours})
    return JSONResponse({"categories": notifier.preferences(), "quiet_hours": notifier.settings.notify_quiet_hours})


async def calendars_list(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        return JSONResponse({"calendars": await services.calendar_choices()})
    except GoogleError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def calendars_save(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        return JSONResponse({"error": "ids required"}, status_code=400)
    return JSONResponse({"ids": request.app.state.services.set_calendars(ids)})


async def status(request: Request) -> Response:
    return JSONResponse(await request.app.state.services.status())


async def run_job(request: Request) -> Response:
    name = request.path_params["name"]
    if not request.app.state.services.trigger(name):
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse({"ok": True})


async def notifications(request: Request) -> Response:
    rows = request.app.state.services.db.all(
        "SELECT id, ts, title, message, priority, url, status, error FROM notifications ORDER BY id DESC LIMIT 100")
    return JSONResponse([dict(r) for r in rows])


async def test_notification(request: Request) -> Response:
    status_ = await request.app.state.services.notifier.notify("Jarvis test", "Notifications are working.", 3)
    return JSONResponse({"status": status_})


async def vault_changes(request: Request) -> Response:
    services: Services = request.app.state.services
    rows = services.db.all("SELECT id, ts, path, actor, before IS NULL AS created FROM note_history "
                           "ORDER BY id DESC LIMIT 150")
    items = [dict(r) | {"url": services.assistant.obsidian_url(r["path"])} for r in rows]
    return JSONResponse({"changes": items, "pending": services.writer.pending()})


async def vault_revert(request: Request) -> Response:
    try:
        path = await request.app.state.services.writer.revert(int(request.path_params["id"]))
    except (VaultError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return JSONResponse({"ok": True, "path": path})


async def google_start(request: Request) -> Response:
    try:
        return RedirectResponse(request.app.state.services.oauth.authorization_url())
    except GoogleError as error:
        return PlainTextResponse(str(error), status_code=400)


async def google_callback(request: Request) -> Response:
    services: Services = request.app.state.services
    if request.query_params.get("error"):
        return RedirectResponse("/#status?google=" + request.query_params["error"])
    try:
        await services.oauth.complete(request.query_params.get("code", ""), request.query_params.get("state", ""))
    except GoogleError as error:
        return PlainTextResponse(f"Google connection failed: {error}", status_code=400)
    services.trigger("gmail")
    services.trigger("calendar")
    return RedirectResponse("/#status")


def create_app(settings: Settings | None = None, services: Services | None = None, start_jobs: bool = True) -> Starlette:
    settings = settings or Settings.from_env()
    services = services or Services(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if start_jobs:
            services.start()
        yield
        await services.stop()
        await http.aclose_all()

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/manifest.webmanifest", manifest),
            Route("/sw.js", service_worker),
            Route("/healthz", healthz),
            Route("/api/session", session),
            Route("/api/login", login, methods=["POST"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/chat/history", chat_history),
            Route("/api/chat/clear", chat_clear, methods=["POST"]),
            Route("/api/activity", activity),
            Route("/api/events", events_list),
            Route("/api/events/{id:int}/add", event_add, methods=["POST"]),
            Route("/api/events/{id:int}/dismiss", event_dismiss, methods=["POST"]),
            Route("/api/events/senders", event_senders),
            Route("/api/events/senders/unmute", event_sender_unmute, methods=["POST"]),
            Route("/api/tts", tts, methods=["POST"]),
            Route("/api/diag/logs", diag_logs),
            Route("/api/diag/traces", diag_traces),
            Route("/api/diag/meta", diag_meta),
            Route("/api/diag/verbose", diag_verbose, methods=["POST"]),
            Route("/api/diag/client", diag_client, methods=["POST"]),
            Route("/api/diag/export", diag_export),
            Route("/api/scheduled", scheduled_list),
            Route("/api/scheduled/{id:int}/confirm", scheduled_confirm, methods=["POST"]),
            Route("/api/scheduled/{id:int}/cancel", scheduled_cancel, methods=["POST"]),
            Route("/api/brief", brief_now, methods=["POST"]),
            Route("/api/status", status),
            Route("/api/calendars", calendars_list),
            Route("/api/notifications/settings", notify_prefs, methods=["GET", "POST"]),
            Route("/api/calendars", calendars_save, methods=["POST"]),
            Route("/api/jobs/{name}/run", run_job, methods=["POST"]),
            Route("/api/notifications", notifications),
            Route("/api/notifications/test", test_notification, methods=["POST"]),
            Route("/api/vault/changes", vault_changes),
            Route("/api/vault/revert/{id:int}", vault_revert, methods=["POST"]),
            Route("/auth/google/start", google_start),
            Route("/auth/google/callback", google_callback),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ],
        middleware=[Middleware(Guard)],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.services = services
    app.state.auth = Auth(services.db)
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*",
                log_level="info")


if __name__ == "__main__":
    run()
