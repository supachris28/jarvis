"""HTTP API and PWA for Jarvis (Starlette)."""

from __future__ import annotations

import contextlib
import json
import os
import re
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
from ..hooks import EVENTS
from ..services import Services
from ..tts import SpeechError
from ..ha import HAError
from ..vault.client import VaultError
from ..vault.markdown import split_frontmatter

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "jarvis_session"
PUBLIC_PATHS = {"/", "/healthz", "/api/session", "/api/login", "/manifest.webmanifest", "/sw.js", "/share-target",
                "/auth/google/callback"}  # callback is protected by the OAuth state from /auth/google/start

QUIET_PATHS = {"/api/chat", "/api/activity", "/api/session", "/api/status", "/api/chat/history", "/api/tts"}

CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; media-src 'self' blob: data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        auth: Auth = request.app.state.auth
        if path.startswith("/api/hook/"):  # Home Assistant: its own token instead of a session (and no CSRF header)
            return await call_next(request)
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
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


async def index(request: Request) -> Response:
    # versioned script/style URLs so a new release is never run with a stale app.js from the browser cache
    from .. import __version__
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for asset in ("/static/app.js", "/static/lights.js", "/static/style.css", "/static/vendor/marked.umd.js", "/static/vendor/purify.min.js"):
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
        "stt": "server" if request.app.state.services.hearing.configured else "browser",
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
    allowed = {k: overrides[k] for k in ("title", "start", "end", "location", "notes", "all_day", "calendar_id")
               if k in overrides}
    try:
        item = await services.events.accept(int(request.path_params["id"]), allowed)
    except GoogleError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    services.trigger("calendar")
    return JSONResponse(item)


async def events_add_all(request: Request) -> Response:
    """'Add all' for several dates from one email."""
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        body = {}
    ids = [int(i) for i in (body or {}).get("ids", []) if str(i).isdigit()][:40]
    calendar_id = str((body or {}).get("calendar_id", "") or "")
    added, errors = 0, []
    for proposal_id in ids:
        try:
            await services.events.accept(proposal_id, {"calendar_id": calendar_id} if calendar_id else {})
            added += 1
        except GoogleError as error:
            errors.append(str(error))
            if "permission" in str(error).casefold():
                break
    if added:
        services.trigger("calendar")
    return JSONResponse({"added": added, "errors": errors[:3]}, status_code=200 if added or not errors else 400)


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


async def email_thread(request: Request) -> Response:
    thread_id = request.path_params["thread_id"]
    if thread_id == "note":  # a vault email note ([[Sources/Email/…]] in a brief or save report) → its thread
        path = request.query_params.get("path", "").strip()
        row = request.app.state.services.db.one(
            "SELECT thread_id FROM threads WHERE path = ? OR path = ? || '.md'", (path, path))
        if row is None:
            return JSONResponse({"error": "Jarvis doesn't know which email that note is for."}, status_code=404)
        thread_id = row["thread_id"]
    if not re.fullmatch(r"[0-9a-fA-F]{6,32}", thread_id):
        return JSONResponse({"error": "not a Gmail thread id"}, status_code=400)
    data = await request.app.state.services.email_thread(thread_id)
    return JSONResponse(data, status_code=200 if data["messages"] else 404)


def _note_candidates(db, target: str) -> list[str]:
    """'People/Ben Topliss', 'Ben Topliss' or 'Ben Topliss.md' → vault paths to try, best first."""
    target = target.split("#")[0].split("|")[0].strip().strip("/")
    if not target:
        return []
    paths = [target if target.casefold().endswith(".md") else target + ".md"]
    name = paths[0].rsplit("/", 1)[-1][:-3]
    try:  # a bare [[Name]] link: find the note by its title in the index
        rows = db.all("SELECT path FROM vault_notes WHERE path = ? COLLATE NOCASE OR title = ? COLLATE NOCASE "
                      "OR path LIKE ? ORDER BY length(path) LIMIT 5", (paths[0], name, "%/" + name + ".md"))
        paths += [r["path"] for r in rows if r["path"] not in paths]
    except Exception:  # noqa: BLE001 — the Obsidian backend has no index; the exact path still works
        pass
    return paths


async def vault_note(request: Request) -> Response:
    """A vault note to read in Jarvis (phones have no Obsidian link handler). Read-only; .md notes only."""
    services: Services = request.app.state.services
    target = request.query_params.get("path", "")[:400]
    for path in _note_candidates(services.db, target):
        parts = path.split("/")
        if any(p.startswith(".") or p in ("", "..") for p in parts):
            continue
        try:
            text = await services.vault.get_text(path)
        except VaultError as error:
            if "not mounted" in str(error) or "offline" in str(error).casefold():
                return JSONResponse({"error": str(error)}, status_code=503)
            continue
        if text is None:
            continue
        frontmatter, body = split_frontmatter(text)
        properties = json.loads(json.dumps(frontmatter or {}, default=str))
        return JSONResponse({"path": path, "title": path.rsplit("/", 1)[-1].removesuffix(".md"), "body": body,
                             "properties": properties, "obsidian_url": services.assistant.obsidian_url(path)})
    return JSONResponse({"error": f"There's no note called {target!r} in the vault."}, status_code=404)


async def ocr(request: Request) -> Response:
    """A shared screenshot → its text (Tesseract on the server)."""
    from ..ocr import OCRError, read_image
    try:
        text = await read_image(await request.body())
    except OCRError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    diag.event("ocr", f"read {len(text)} characters from a shared image")
    return JSONResponse({"text": text})


def _multipart_fields(body: bytes, content_type: str) -> dict[str, str]:
    """Text fields of a multipart form (Starlette's own parser needs python-multipart, which isn't installed)."""
    match = re.search(r"boundary=\"?([^\";]+)", content_type)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for part in body.split(b"--" + match.group(1).encode())[1:-1]:
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name and b"filename=" not in head:
            fields[name.group(1).decode(errors="replace")] = value[:-2].decode("utf-8", errors="replace")[:4000]
    return fields


async def share_target(request: Request) -> Response:
    """Android's share menu posts here. Normally the service worker answers first (and keeps any image); this is
    the fallback, so shared text still arrives when it isn't running. It only redirects — nothing is stored."""
    from urllib.parse import urlencode
    body = await request.body()
    fields = _multipart_fields(body[:2_000_000], request.headers.get("content-type", "")) if body else {}
    query = {f"share_{k}": fields.get(k, "") for k in ("title", "text", "url") if fields.get(k)}
    if b"filename=" in body[:2_000_000]:
        query["share_note"] = "image"
    return RedirectResponse("/?" + urlencode(query), status_code=303)


async def hook(request: Request) -> Response:
    """Home Assistant → Jarvis (Status → Home Assistant triggers). Token as 'Authorization: Bearer …' or ?token=."""
    hooks = request.app.state.services.hooks
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if not hooks.check(given):
        diag.warning("hook", f"rejected a Home Assistant trigger with a wrong token from {client_ip(request)}")
        return JSONResponse({"error": "wrong token"}, status_code=401)
    result = await hooks.fire(request.path_params["event"])
    return JSONResponse(result, status_code=404 if result.get("error") else 200)


async def hook_settings(request: Request) -> Response:
    hooks = request.app.state.services.hooks
    if request.method == "POST":
        hooks.new_token()
    base = request.app.state.settings.public_url.rstrip("/")
    return JSONResponse({"token": hooks.token, "events": list(EVENTS),
                         "url": f"{base}/api/hook/", "brief_time": request.app.state.settings.brief_time})


async def vault_note_append(request: Request) -> Response:
    """Add a line to a note from the note reader (your own words; undo from the reader or the Vault tab)."""
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    path, text = str(body.get("path", ""))[:400], str(body.get("text", ""))[:4000]
    parts = path.split("/")
    if not path.endswith(".md") or any(p.startswith(".") or p in ("", "..") for p in parts):
        return JSONResponse({"error": "not a note"}, status_code=400)
    if not text.strip():
        return JSONResponse({"error": "Nothing to add."}, status_code=400)
    try:
        change = await services.writer.append_text(path, text)
    except VaultError as error:
        return JSONResponse({"error": str(error)}, status_code=503 if "reach" in str(error) else 400)
    return JSONResponse({"ok": True, "change_id": change})


async def changelog(request: Request) -> Response:
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    return JSONResponse({"version": __version__, "markdown": text})


async def people_list(request: Request) -> Response:
    return JSONResponse(await request.app.state.services.people.directory())


def _person_path(path: str) -> str | None:
    path = path.strip()[:300]
    parts = path.split("/")
    if not path.startswith("People/") or not path.endswith(".md") or any(p.startswith(".") or p in ("", "..") for p in parts):
        return None
    return path


async def person_detail(request: Request) -> Response:
    path = _person_path(request.query_params.get("path", ""))
    person = await request.app.state.services.people.person(path) if path else None
    if not person:
        return JSONResponse({"error": "Unknown person."}, status_code=404)
    person["briefing"] = await request.app.state.services.people.briefing(path)
    return JSONResponse(person)


async def person_update(request: Request) -> Response:
    people = request.app.state.services.people
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    fields = {k: body.get(k) for k in ("relation", "family", "birthday", "partner", "children", "parents", "phone")
              if k in body}
    try:
        if body.get("not_children") and _person_path(str(body.get("path", ""))):
            people.dismiss_children(body["path"], [c for c in body["not_children"] if _person_path(str(c))][:20])
            return JSONResponse({"ok": True})
        if body.get("skip") and _person_path(str(body.get("path", ""))):
            people.skip(body["path"])
            return JSONResponse({"ok": True})
        if body.get("name") and not body.get("path"):
            return JSONResponse(await people.create(str(body["name"])[:80], fields))
        path = _person_path(str(body.get("path", "")))
        if not path:
            return JSONResponse({"error": "Unknown person."}, status_code=400)
        return JSONResponse(await people.update(path, fields))
    except VaultError as error:
        return JSONResponse({"error": str(error)}, status_code=503)


async def meetings_list(request: Request) -> Response:
    from ..pipelines.people import upcoming_meetings
    days = min(31, max(1, int(request.query_params.get("days", "7") or 7)))
    return JSONResponse({"meetings": await upcoming_meetings(request.app.state.services.people, days)})


async def people_link(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    path = str(body.get("path", ""))
    if path and not _person_path(path):
        return JSONResponse({"error": "Unknown person."}, status_code=400)
    try:
        return JSONResponse(await request.app.state.services.people.link_name(
            str(body.get("name", "")), path, bool(body.get("create")), bool(body.get("not_person"))))
    except VaultError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def tasks_list(request: Request) -> Response:
    tasks = request.app.state.services.tasks
    shopping, where, problem = [], "", ""
    try:
        shopping, where = await tasks.shopping()
    except HAError as error:
        problem = str(error)
    return JSONResponse({"tasks": tasks.open(include_snoozed=True), "shopping": shopping, "shopping_where": where,
                         "shopping_problem": problem})


async def tasks_change(request: Request) -> Response:
    tasks = request.app.state.services.tasks
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if request.method == "DELETE":
        tasks.delete(request.path_params["id"])
        return JSONResponse({"ok": True})
    if "done" in body:
        return JSONResponse(tasks.complete(request.path_params["id"], bool(body["done"])) or {})
    return JSONResponse(tasks.update(request.path_params["id"], body) or {})


async def tasks_add(request: Request) -> Response:
    from ..pipelines.tasks import parse_task, split_items
    tasks = request.app.state.services.tasks
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str((body or {}).get("text", "")).strip()[:300]
    if not text:
        return JSONResponse({"error": "Nothing to add."}, status_code=400)
    if (body or {}).get("list") == "shopping":
        try:
            where = await tasks.add_shopping(split_items(text))
        except HAError as error:
            return JSONResponse({"error": str(error)}, status_code=503)
        return JSONResponse({"ok": True, "where": where})
    parsed = parse_task(f"todo: {text}", tasks.today) or (text[:1].upper() + text[1:], None)
    due = (body or {}).get("due") or (parsed[1].isoformat() if parsed[1] else "")
    task = tasks.add(parsed[0])
    if due:
        task = tasks.update(task["id"], {"due": due})
    return JSONResponse(task)


async def shopping_tick(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    try:
        ok = await request.app.state.services.tasks.tick_shopping(str((body or {}).get("item", ""))[:200])
    except HAError as error:
        return JSONResponse({"error": str(error)}, status_code=503)
    return JSONResponse({"ok": ok})


async def deadlines_list(request: Request) -> Response:
    deadlines = request.app.state.services.deadlines
    return JSONResponse({"deadlines": deadlines.upcoming(), "waiting": deadlines.waiting(),
                         "followup_days": request.app.state.settings.followup_days,
                         "looked_back": bool(request.app.state.services.db.get("deadlines.looked_back"))})


async def deadline_update(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    ok = request.app.state.services.deadlines.set_status(request.path_params["id"], str((body or {}).get("status", "")))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)


async def followup_dismiss(request: Request) -> Response:
    thread_id = request.path_params["thread_id"]
    if not re.fullmatch(r"[0-9a-fA-F]{6,32}", thread_id):
        return JSONResponse({"error": "not a Gmail thread id"}, status_code=400)
    request.app.state.services.deadlines.dismiss_waiting(thread_id)
    return JSONResponse({"ok": True})


async def deadlines_look_back(request: Request) -> Response:
    try:
        return JSONResponse(await request.app.state.services.deadlines.look_back())
    except GoogleError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def events_rescan(request: Request) -> Response:
    try:
        return JSONResponse(await request.app.state.services.rescan_recent_events(6))
    except GoogleError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def email_find_events(request: Request) -> Response:
    thread_id = request.path_params["thread_id"]
    if not re.fullmatch(r"[0-9a-fA-F]{6,32}", thread_id):
        return JSONResponse({"error": "not a Gmail thread id"}, status_code=400)
    try:
        return JSONResponse(await request.app.state.services.find_events_in_thread(thread_id))
    except GoogleError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def deliveries_list(request: Request) -> Response:
    return JSONResponse({"items": request.app.state.services.deliveries.active()})


async def deliveries_add(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        body = {}
    item = services.deliveries.add_from_chat(str((body or {}).get("text", ""))[:500])
    if item.get("error"):
        return JSONResponse(item, status_code=400)
    if item["tracking_url"]:
        await services.deliveries.check(item["id"])
    services.deliveries.quiet(item["id"])
    return JSONResponse(services.deliveries.get(item["id"]))


async def deliveries_look_back(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        body = {}
    days = max(1, min(365, int((body or {}).get("days", 30) or 30)))
    try:
        result = await services.deliveries.look_back(days)
    except GoogleError as error:
        result = {"error": str(error)}
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


async def delivery_check(request: Request) -> Response:
    services: Services = request.app.state.services
    delivery_id = int(request.path_params["id"])
    services.db.execute("UPDATE deliveries SET poll = 1, check_failures = 0, poll_note = '' WHERE id = ?", (delivery_id,))
    result = await services.deliveries.check(delivery_id)
    await services.deliveries.flush_notifications()
    return JSONResponse({**result, "item": services.deliveries.get(delivery_id)})


async def delivery_archive(request: Request) -> Response:
    request.app.state.services.deliveries.archive(int(request.path_params["id"]))
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


async def feedback_add(request: Request) -> Response:
    """'That was wrong': keep the question, the answer, a note and what Jarvis did (its log entries)."""
    services: Services = request.app.state.services
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    trace = str(body.get("trace", ""))[:64]
    answer = str(body.get("answer", ""))[:8000]
    prompt = ""
    if trace:
        row = services.db.one("SELECT content FROM chat_messages WHERE trace = ? AND role = 'user' ORDER BY id LIMIT 1",
                              (trace,))
        prompt = row["content"] if row else ""
        if not answer:
            row = services.db.one("SELECT content FROM chat_messages WHERE trace = ? AND role != 'user' "
                                  "ORDER BY id DESC LIMIT 1", (trace,))
            answer = row["content"] if row else ""
    prompt = prompt or str(body.get("prompt", ""))  # replies that weren't saved (e.g. errors): the app sends it
    if not (trace or answer):
        return JSONResponse({"error": "nothing to report"}, status_code=400)
    logs = [_log_row(r) for r in services.db.all("SELECT * FROM logs WHERE trace = ? ORDER BY id LIMIT 400", (trace,))] \
        if trace else []
    cursor = services.db.execute(
        "INSERT INTO feedback (ts, trace, prompt, answer, route, note, version, logs) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), trace, prompt[:4000], answer, str(body.get("route", ""))[:40], str(body.get("note", ""))[:2000],
         __version__, json.dumps(logs, ensure_ascii=False, default=str)))
    diag.event("feedback", f"answer reported as wrong: {prompt[:80] or answer[:80]}", note=str(body.get("note", ""))[:200])
    return JSONResponse({"id": cursor.lastrowid})


def _feedback_rows(services: Services, status: str = "", with_logs: bool = False) -> list[dict]:
    rows = services.db.all("SELECT * FROM feedback" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC "
                           "LIMIT 200", (status,) if status else ())
    items = []
    for row in rows:
        item = dict(row)
        logs = json.loads(item.pop("logs") or "[]")
        if with_logs:
            item["logs"] = logs
        else:
            item["log_count"] = len(logs)
        items.append(item)
    return items


async def feedback_list(request: Request) -> Response:
    services: Services = request.app.state.services
    status = request.query_params.get("status", "")
    status = status if status in ("open", "fixed", "dismissed") else ""
    counts = {r["status"]: r["n"] for r in services.db.all("SELECT status, COUNT(*) n FROM feedback GROUP BY status")}
    return JSONResponse({"items": _feedback_rows(services, status), "counts": counts})


async def feedback_update(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    status = str((body or {}).get("status", ""))
    if status not in ("open", "fixed", "dismissed"):
        return JSONResponse({"error": "status must be open, fixed or dismissed"}, status_code=400)
    request.app.state.services.db.execute("UPDATE feedback SET status = ? WHERE id = ?",
                                          (status, request.path_params["id"]))
    return JSONResponse({"ok": True})


async def feedback_export(request: Request) -> Response:
    """Every open report with its log entries, as one file to hand over for fixing."""
    services: Services = request.app.state.services
    body = json.dumps({"version": __version__, "exported": time.time(),
                       "reports": _feedback_rows(services, request.query_params.get("status", "open"), True)},
                      ensure_ascii=False, indent=1, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="jarvis-reports-{time.strftime("%Y%m%d")}.json"'})


MAX_AUDIO = 4 * 1024 * 1024  # a minute of speech is well under 1 MB; SWAG allows 5 MB


async def stt(request: Request) -> Response:
    """🎤: a short recording → text (Whisper on the PC). 503 tells the app to use the phone's recognition."""
    hearing = request.app.state.services.hearing
    if not hearing.configured:
        return JSONResponse({"error": "Whisper isn't set up", "fallback": True}, status_code=503)
    audio = await request.body()
    if not audio:
        return JSONResponse({"error": "no audio"}, status_code=400)
    if len(audio) > MAX_AUDIO:
        return JSONResponse({"error": "recording too long"}, status_code=413)
    try:
        text = await hearing.transcribe(audio, request.headers.get("content-type", "audio/webm"))
    except SpeechError as error:
        diag.warning("stt", str(error))
        return JSONResponse({"error": str(error), "fallback": True}, status_code=503)
    diag.event("stt", f"heard {len(text)} characters", bytes=len(audio))
    return JSONResponse({"text": text})


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


async def calendar_targets(request: Request) -> Response:
    try:
        return JSONResponse({"calendars": await request.app.state.services.events.target_calendars()})
    except GoogleError as error:
        return JSONResponse({"error": str(error), "calendars": []}, status_code=400)


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
        "SELECT id, ts, title, message, priority, url, status, error FROM notifications WHERE status != 'off' "
        "ORDER BY id DESC LIMIT 100")
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
            Route("/api/events/add-all", events_add_all, methods=["POST"]),
            Route("/api/events/rescan", events_rescan, methods=["POST"]),
            Route("/api/events/{id:int}/dismiss", event_dismiss, methods=["POST"]),
            Route("/api/events/senders", event_senders),
            Route("/api/events/senders/unmute", event_sender_unmute, methods=["POST"]),
            Route("/api/tts", tts, methods=["POST"]),
            Route("/api/stt", stt, methods=["POST"]),
            Route("/api/diag/logs", diag_logs),
            Route("/api/diag/traces", diag_traces),
            Route("/api/diag/meta", diag_meta),
            Route("/api/diag/verbose", diag_verbose, methods=["POST"]),
            Route("/api/diag/client", diag_client, methods=["POST"]),
            Route("/api/diag/export", diag_export),
            Route("/api/scheduled", scheduled_list),
            Route("/api/email/{thread_id}", email_thread),
            Route("/api/email/{thread_id}/events", email_find_events, methods=["POST"]),
            Route("/api/changelog", changelog),
            Route("/api/people", people_list),
            Route("/api/people/one", person_detail),
            Route("/api/people/link", people_link, methods=["POST"]),
            Route("/api/meetings", meetings_list),
            Route("/api/people", person_update, methods=["POST"]),
            Route("/api/tasks", tasks_list),
            Route("/api/tasks", tasks_add, methods=["POST"]),
            Route("/api/tasks/{id:int}", tasks_change, methods=["POST", "DELETE"]),
            Route("/api/shopping/tick", shopping_tick, methods=["POST"]),
            Route("/api/deadlines", deadlines_list),
            Route("/api/deadlines/look-back", deadlines_look_back, methods=["POST"]),
            Route("/api/deadlines/{id:int}", deadline_update, methods=["POST"]),
            Route("/api/followups/{thread_id}/dismiss", followup_dismiss, methods=["POST"]),
            Route("/api/deliveries", deliveries_list),
            Route("/api/deliveries", deliveries_add, methods=["POST"]),
            Route("/api/deliveries/{id:int}/check", delivery_check, methods=["POST"]),
            Route("/api/deliveries/look-back", deliveries_look_back, methods=["POST"]),
            Route("/api/deliveries/{id:int}/archive", delivery_archive, methods=["POST"]),
            Route("/api/scheduled/{id:int}/confirm", scheduled_confirm, methods=["POST"]),
            Route("/api/scheduled/{id:int}/cancel", scheduled_cancel, methods=["POST"]),
            Route("/api/brief", brief_now, methods=["POST"]),
            Route("/api/status", status),
            Route("/api/calendars", calendars_list),
            Route("/api/calendars/targets", calendar_targets),
            Route("/api/notifications/settings", notify_prefs, methods=["GET", "POST"]),
            Route("/api/calendars", calendars_save, methods=["POST"]),
            Route("/api/jobs/{name}/run", run_job, methods=["POST"]),
            Route("/api/notifications", notifications),
            Route("/api/notifications/test", test_notification, methods=["POST"]),
            Route("/api/vault/changes", vault_changes),
            Route("/api/vault/note", vault_note),
            Route("/api/vault/note/append", vault_note_append, methods=["POST"]),
            Route("/api/hook/{event}", hook, methods=["POST"]),
            Route("/api/ocr", ocr, methods=["POST"]),
            Route("/share-target", share_target, methods=["POST"]),
            Route("/api/hooks", hook_settings, methods=["GET", "POST"]),
            Route("/api/feedback", feedback_add, methods=["POST"]),
            Route("/api/feedback", feedback_list),
            Route("/api/feedback/export", feedback_export),
            Route("/api/feedback/{id:int}", feedback_update, methods=["POST"]),
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
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("JARVIS_TRUSTED_PROXIES", "*"),
                log_level="info")


if __name__ == "__main__":
    run()
