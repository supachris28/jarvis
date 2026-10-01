# Code review: optimisations and off-the-shelf replacements (v0.8)

Branch `review/optimise-and-offtheshelf`, reviewed 1 October 2026 against `cbfdc70` (Version 0.8). About 8,000 lines of Python in `src/jarvis`, 52 tests, all passing before and after.

## Summary

Jarvis is in better shape than most projects of its size. The code is compact, the tests cover the intent-heavy parts (date phrasings, routing, Home Assistant commands, vault indexing, SSRF), and the architecture decisions (scripts before models, outbox to Nextcloud, read-only container) are sound. Most of the custom code that *looks* like it should be a library is custom for a reason: it encodes UK-specific or Jarvis-specific behaviour that a general library would get wrong or make harder to explain.

The real cost was not in reinventing libraries but in a handful of hot paths: a new HTTPS connection for every outgoing request, a few full-table scans that run per email during a backfill, and one SQLite commit per statement when indexing the vault. Those are fixed on this branch. Three library swaps are worth making (`icalendar`, a proper HTML extractor, and a real Markdown renderer in the browser); they could not be implemented here because PyPI is blocked from this session, so each one below comes with a ready-to-apply sketch.

## What changed on this branch

Six commits, each independently revertable. Tests pass after every one.

| Commit | Change | Effect |
| --- | --- | --- |
| `d7d5d70` | `jarvis/http.py`: one pooled `httpx.AsyncClient` per (timeout, verify) pair, closed in the app lifespan. Replaces 20 `async with httpx.AsyncClient(...)` blocks across 11 modules. | No TCP + TLS handshake per request. A 500-message Gmail backfill was 500 handshakes to googleapis.com; Home Assistant was polled with a fresh TLS context every 30 s. The `diag` hook on `AsyncClient.send` is untouched. Also: a Google 401 now drops the cached access token so the next call refreshes it, instead of re-reading the same rejected token from disk forever. |
| `602a82a` | SQL hot paths: `gmail` one `IN (...)` lookup instead of one `SELECT` per message id; `calendar.remind` no longer loads every past event every minute; `substr(start, 1, 10) = ?` rewritten as `start >= day AND start < next_day` (equivalent on ISO text, proven in a test harness) with indexes on `events(start)`, `event_proposals(start)`, `event_proposals(status)`, `scheduled(status, due)`, `notifications(status, ts)`; `Database.transaction()` groups each note's 6+ index statements into one commit. | Indexing 300 notes: 0.68 s → 0.30 s in a tmpfs; on the server's disk the win is larger because each autocommit was a WAL fsync. During a backfill `propose()` ran two full scans per candidate. |
| `6fbf733` | `writer.apply` normalises CRLF before the "unchanged" comparison. | A note with Windows line endings was rewritten and logged to `note_history` on every flush. |
| `91f503d` | Monthly repeats remember the day of the month they were set for (stored in `payload.day_of_month`). SSRF guard uses `is_global` and unwraps IPv4-mapped IPv6. `/api/diag` validates `?before=`/`?limit=`. ntfy click URL uses `rstrip("/")`. | "Remind me on the 31st every month" became the 28th for good after February (Jan 31 → Feb 28 → Mar 28 …). `is_private` does not cover 100.64.0.0/10 (carrier-grade NAT — Tailscale) so a web lookup could be pointed at a Tailscale address. |
| `81f3f1a` | Home Assistant taught names cached per diagnostic trace; the per-entity word-frequency table built once per states snapshot. | Every chat message asked for the names 2–4 times (routing, gathering, resolving), each a DB query plus a vault read of `Jarvis/Home names.md`, and rebuilt the word table. Caching per trace means a new turn still sees names you taught or typed into the note immediately. |
| `c6154f9` | Dead code: `calendar.parse_when` (a copy of `extract.events.parse_iso`), `_answer(search_query=)`, `HomeAssistant.match()`, `EventCandidate.extra`. | Less to read. |

## Recommended library swaps

Ranked by value. None could be installed in this session (PyPI returns 403 through the egress proxy), so run `scripts/test.ps1` after applying each.

### 1. `icalendar` for `extract/events.py` `parse_ics` — adopt

Replaces about 90 lines: `_unfold`, `_ics_text`, `_ics_time`, `parse_ics`, `_zone` and the ten-entry `WINDOWS_TZ` map. The hand parser has three weaknesses that invites actually hit: `line.partition(":")` breaks when a parameter value is quoted and contains a colon (some exporters quote TZIDs like `"(UTC+00:00) Dublin, Edinburgh, Lisbon, London"`); `;` inside quoted parameters breaks the parameter split; and `VTIMEZONE` blocks are ignored, so any `TZID` not in the ten-entry map silently falls back to your own timezone, which is wrong for an invitation from abroad. `icalendar` is at 7.3.0 (August 2026), pure Python, uses `zoneinfo`, and ships the full Windows → IANA map.

Keep `EventCandidate`, the `STATUS:CANCELLED` / `METHOD:CANCEL` checks, and the existing five tests in `test_events_voice.py`, which define the behaviour.

```python
from icalendar import Calendar

def parse_ics(text: str, default_tz: tzinfo) -> list[EventCandidate]:
    try:
        calendar = Calendar.from_ical(text)
    except ValueError:
        return []
    method = str(calendar.get("METHOD", "")).upper()
    events = []
    for component in calendar.walk("VEVENT"):
        if method == "CANCEL" or str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = component.decoded("DTSTART", None)
        if start is None:
            continue
        all_day = not isinstance(start, datetime)
        end = component.decoded("DTEND", None)
        if end is None:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        events.append(EventCandidate(
            title=str(component.get("SUMMARY", "")) or "Event",
            start=_iso(start, default_tz), end=_iso(end, default_tz), all_day=all_day,
            location=str(component.get("LOCATION", "")), notes=str(component.get("DESCRIPTION", ""))[:2000],
            ical_uid=str(component.get("UID", "")), confidence=1.0, source="ics"))
    return events

def _iso(value, default_tz):
    if not isinstance(value, datetime):
        return value.isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_tz)
    return value.astimezone(default_tz).isoformat()
```

Risk: low. Watch for `RRULE`: like today, a recurring invitation yields one event (the first), which is fine for a proposal card.

### 2. A real HTML-to-text extractor for `websearch.page_text` and `gmail.html_to_text` — adopt

Two separate regex strippers exist. For web pages the regex version leaves cookie banners, navigation and comment threads in the text that `best_passages` scores, which directly lowers the quality of what the model sees. Two options:

- **trafilatura** (`trafilatura.extract(html, include_comments=False, favor_precision=True)`) does boilerplate removal properly and replaces `page_text` plus most of `best_passages`' job. Cost: pulls in lxml, roughly 25 MB more image.
- **selectolax** (`HTMLParser(html).body.text(separator="\n")`) is small, fast and has no lxml dependency; it gives correct tag and entity handling for both files but no boilerplate removal.

trafilatura is the quality win for web lookups; selectolax is the cheap correctness win for email bodies. Doing both is reasonable. Keep the SSRF guard, the size and redirect limits, and `best_passages` scoring on top.

### 3. `marked` + `DOMPurify` in the browser (vendored under `/static`) — adopt

The 27-line renderer in `app.js` has no fenced code blocks (``` lines render as a paragraph), no tables, no italics, ordered lists become `<ul>`, nested lists flatten, and the `[1]` citation rewrite at `app.js:237` runs on `innerHTML`, so it can touch attributes. LLM output uses all of those. The CSP is `script-src 'self'`, so vendor the two files (about 45 KB together) rather than loading from a CDN; the `[[wikilink]]` handling becomes a small `marked` extension. Pin the versions and check DOMPurify's advisories when you vendor it; it is a frequent CVE subject precisely because it is the sanitiser everyone relies on.

### 4. `watchfiles` for the vault index — adopt when convenient

`uvicorn[standard]` already installs `watchfiles` (Rust inotify bindings). The `index` job walks and `stat`s the whole vault every minute; `watchfiles.awatch("/vault")` would index only what changed, with a full walk kept as a ten-minute fallback. Bind mounts on the same host propagate inotify events. Zero new dependencies.

### 5. `python-dateutil` — only for one thing

`relativedelta(months=+1, day=31)` clamps to the end of the month and would be the idiomatic fix for monthly recurrence; the branch fixes it without the dependency. `dateutil.parser` is *not* a good replacement for `facts.parse_date`: the custom parser's point is distinguishing "year unknown" (`--03-12`, "12 March") from a stated year, which `dateutil` cannot express.

### Not recommended

| Library | For | Why not |
| --- | --- | --- |
| `dateparser` | `extract/when.py`, `event_text.py` | Covers about half the cases (no ranges like "7–9pm", no "for 2 hours", no "every weekday", no title extraction), is slow (tens of ms per call), and happily parses "May" in a person's name. The 15-case test suite already pins UK-specific intent. Better: merge the three date grammars (see below). |
| `APScheduler` | `services.py` job loop | The loop is 30 lines, supports manual triggering via `asyncio.Event`, and records last result/trace per job. APScheduler would add persistence you do not need (jobs are static) and lose the trace integration. |
| `pyotp` | `auth.totp_code` | The 10-line RFC 6238 implementation is correct and tested. pyotp would save nothing. |
| `pydantic-settings` | `config.py` | Would shrink `from_env` by about 70 lines and give typed validation, but pulls in pydantic (several MB) for a file that changes rarely. Reasonable if you later want a settings UI; not now. |
| `python-frontmatter` | `vault/markdown.py` | Reformats YAML on round-trip, which would make every note "changed". The 20 lines here are right. |
| `webdav4` / `aiodav` | `vault/webdav.py` | The 86 lines are mostly Nextcloud-specific error messages; the WebDAV part is two verbs. |
| `authlib` / `google-auth` | `google/oauth.py` | google-auth is synchronous; the valuable part of this file (scope diffing, persisting the revocation error) is Jarvis-specific. |
| official `mcp` SDK | `mcp.py` | Heavy (pydantic, anyio) for one read-only client. Fix the one bug instead: SSE parsing only handles single-line `data:` fields. |
| `rapidfuzz` | HA entity matching, `similar_titles` | Token-overlap and `difflib` are adequate at this scale and easier to explain when a match is wrong. |
| `tenacity` | retries | There are no retry loops to replace; the outbox already handles the vault and HA/ntfy are single-shot by design. |
| `SQLAlchemy` / `aiosqlite` | `db.py` | 289 lines of plain SQL with a lock is the right size. SQLite calls are sub-millisecond; the stalls came from N+1 patterns, now fixed. |
| Starlette `BaseHTTPMiddleware` → pure ASGI | `web/app.py` `Guard` | Starlette recommends pure ASGI middleware for streaming responses; the current form runs the endpoint in a separate task. About 20 lines to convert. Not urgent. |

## Further optimisations (not done, in priority order)

1. **Merge the three date grammars.** `extract/when.py` (reminders), `extract/event_text.py` (calendar adds) and `assistant/facts.py` (birthdays) each define month and weekday tables, a clock parser and "roll to next year if past" logic — about 420 lines with subtly different behaviour. Example: `when.py`'s `date_dm` has no year group, so "remind me on 14 October 2027 at 18:30" resolves to 2026 and leaves "2027" in the reminder text; `event_text.py` handles the year. `event_text._find_date` is the most complete; make `when.py` use it. Net removal around 120 lines.
2. **Serve calendar questions from the local `events` table.** `gather_calendar` makes two sequential Google API calls per calendar on every calendar question, although the table already holds −2…+60 days. Fall back to the API only outside that window.
3. **`collect_birthdays`** reads every `People/` note's text (via `get_text`) on every birthday question and every morning brief. With the files backend, `vault_notes.frontmatter` already holds the properties, so property birthdays are one SQL query; only the "Birthday: …" body lines need text. Also exclude `Jarvis/` and `Journal/` paths from the "unparsed" list fed to the model, or Jarvis's own briefs ("Birthdays this week") become model input.
4. **`match_people`** compiles one regex per known person per call and is called two to three times per request. Build one alternation regex when the people index changes.
5. **`refresh_people`** walks `People/` with a file read per note hourly; `SELECT path, frontmatter FROM vault_notes WHERE path LIKE 'People/%'` answers it in one query.
6. **`notes_where`** loads and `json.loads` every note's frontmatter in Python; `json_extract(frontmatter, '$.' || ?)` does it in SQL (the pattern is already used in `writer.py`).
7. **`fetch_page`** does `body += chunk` on up to 1.5 MB (quadratic); use `bytearray`.
8. **`PRAGMA synchronous=NORMAL`** in WAL mode is the standard setting for a state database like this one: safe against application crashes, loses at most the last transactions on power loss. Combined with the new transactions it makes the first vault index several times faster on a real disk. Left as a decision for you because it is a durability trade.
9. **Test suite speed.** 52 tests take 27 s because each test starts real uvicorn servers in threads (for Jarvis and each fake service) and polls 50 ms for readiness. Starlette's `TestClient` would remove most of that; the fakes that need a real socket (Ollama streaming) can keep one.

## Correctness notes not changed (they are product decisions)

- `notify.py`: `dedupe` is a UNIQUE constraint, so a notification that *failed* to send with a given key can never be retried (reminders use `reminder:{id}:{due}`). Either exclude failed rows from the uniqueness check or add a retry sweep.
- `brief.due_now`: if Jarvis is down between `BRIEF_TIME` and noon, that day's brief is skipped rather than sent late.
- `bible.py`: single-word aliases `is`, `am`, `act`, `song`, `job`, `ps`, `pr` plus the ASK list containing `say/says` mean "what does the bill say, is 3 enough?" can resolve to Isaiah 3. Requiring the full book name (or a three-letter alias) when the rest of the prompt is non-empty would fix it.
- `web/app.py`: `forwarded_allow_ips="*"` lets anything that reaches port 8080 directly spoof `X-Forwarded-For` and defeat login throttling. The container publishes no ports and sits on the SWAG network, so this is acceptable today; setting it to the SWAG container or subnet costs nothing.
- `webdav.py` `put` sends no `If-Match`, so an edit made on your phone between Jarvis's read and write is overwritten (Nextcloud's versioning is the safety net). Reads come from the mount, not WebDAV, so an ETag is not available without a PROPFIND per write.
- `compound.py` uses `date.today()` (server-local, UTC in the container) while the rest of the pipeline uses your timezone; near midnight the two disagree.
- `facts.py`: a contact with an empty `path` is keyed by name while a People note is keyed by path, so the same person could appear twice. Latent today because contacts always get a path.

## Dockerfile and compose

The image is already small and well locked down (slim base, deps layer before `COPY src`, non-root, read-only root, all capabilities dropped, stdlib healthcheck). Three small improvements:

- `PYTHONDONTWRITEBYTECODE=1` plus a read-only filesystem means no `.pyc` is ever written, so every cold start compiles everything. Add `RUN python -m compileall -q /app/src` after `COPY src` and drop the env var.
- Pin the base image by digest (`python:3.12-slim@sha256:…`), and pin `kokoro` and `searxng` to versions rather than `:latest`; neither has a healthcheck.
- `compose.yml` still declares an `ntfy-data` volume with no ntfy service, and the `./certs` mount is only for the retired Obsidian REST backend. If you retire `VAULT_BACKEND=obsidian` for good, `vault/client.py` (177 lines), the `search_jsonlogic`/`search_dql` shims in `files.py`, the `OBSIDIAN_*` settings and the `/certs` mount can all go.

## Status after v0.9.5

| Recommendation | Status |
| --- | --- |
| Merge the review branch | Done (v0.9.0). |
| `icalendar` for invites | Done (v0.9.4), with the built-in reader kept as a fallback. A test for quoted parameters and foreign time zones runs in the Docker test stage. |
| Markdown renderer in the browser | Done (v0.9.4): marked 18 + DOMPurify 3, vendored by a Docker build stage; citation links touch text nodes only. |
| HTML extractor | Web pages: trafilatura (v0.9.5), used for internet answers; tracking pages keep the plain stripper. Email bodies: **not changed** — selectolax would change the text every email parser reads (dates, events, deliveries) and could not be checked outside the image. |
| `watchfiles` for the vault index | Not done yet. |
| Merge the date grammars | Done for reminders (v0.9.5): `when.py` reads calendar dates with `event_text._find_date`, so years work ("14 October 2027"). Birthdays keep their own parser on purpose (year-unknown dates). |
| Calendar questions from the local table | Done (v0.9.5): the next three weeks come from `events`; Google is only searched across the wider range. |
| `collect_birthdays` | Done (v0.9.5): properties from the index; only People notes that mention a birthday are read; `Jarvis/` and `Journal/` excluded from mentions. |
| `match_people` | Done (v0.9.5): one compiled pattern per people list (cached). |
| `refresh_people`, `notes_where` | Done (v0.9.5): index queries / `json_extract`. |
| `fetch_page` bytearray | Done (v0.9.5). |
| `PRAGMA synchronous=NORMAL` | Done (v0.9.6), agreed by Chris. |
| Faster test suite | Not done. |
| Notification retry after a failed send | Done (v0.9.5). |
| Brief skipped if Jarvis was down in the morning | Left as is (product decision). |
| Bible short aliases | Done (v0.9.5): one- and two-letter abbreviations only count when the message is just the reference. |
| `forwarded_allow_ips` | Configurable with `JARVIS_TRUSTED_PROXIES` (default `*`, as before). |
| WebDAV `If-Match` | Left as is. |
| `compound.py` timezone | Done (v0.9.5). |
| MCP multi-line SSE `data:` | Done (v0.9.5). |
| Dockerfile: compile at build, test stage | Done (v0.9.4): `deploy.sh` builds `--target test` first and stops on failure. |
| Pin base images by digest; healthchecks for kokoro/searxng | Not done (needs the digests from your registry). |
| Compose: drop `ntfy-data` and `./certs` | Done in the repo template (the server's copy needs the same edit if you want it). |

## Suggested order

1. Merge this branch (everything on it is tested and dependency-free).
2. `icalendar` swap, then run the tests — small, self-contained, fixes real parsing gaps.
3. Browser Markdown renderer — the most visible quality improvement for chat.
4. HTML extractor for web lookups.
5. Merge the date grammars when next touching reminders.
