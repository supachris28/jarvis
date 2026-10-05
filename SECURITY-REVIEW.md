# Jarvis — Security Assessment

**Target:** `jarvis.keeeys.uk` (Jarvis v0.11.0) and the Jarvis source tree (`D:\Dev\jarvis`)
**Date:** 2 October 2026
**Type:** White-hat, grey-box (source + live surface), external-attacker perspective, **non-destructive**
**Assessor:** Claude (automated code review + non-intrusive external recon)

---

## 1. Scope & method

This was a grey-box review from the standpoint of an unauthenticated outsider: what an attacker on the internet, or someone able to send Chris an email, could do against the deployed service — informed by read access to the full source.

**What I did:**

- Static security review of the application (HTTP layer, auth/session, access control, the vault file layer, the Google/Home-Assistant/MCP/web-search integrations, DB access, deployment and reverse-proxy config).
- Non-intrusive external recon against the live host: ordinary HTTPS GETs to public endpoints only (`/`, `/healthz`, `/api/session`).

**What I deliberately did *not* do** (stated up front so the scope is honest): no exploitation, no fuzzing, no brute-force, no injection payloads, no attempt to use or validate the discovered Google token, and no port/network scanning. Everything below is reasoned from code and from benign requests. Items marked *unverified* need a live check you can run.

**Headline:** This is a well-built, security-conscious application. The common classes an external attacker reaches for are mostly closed: SQL is fully parameterised, path traversal in the vault is properly contained, there is a real SSRF guard, CSRF is defended with a custom header + Origin check, sessions are hashed server-side, secrets are kept out of git and the image, and the container is hardened (read-only rootfs, all caps dropped, `no-new-privileges`). There is **no** `eval`/`exec`/`subprocess`/`pickle`/`yaml.load`/`shell=True` anywhere, and no disabled TLS verification. The findings are refinements, not open doors — the two "Medium" items are the ones worth acting on.

---

## 2. Findings at a glance

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1 | **Medium** | Blind SSRF reachable by email: auto-created delivery polls an email-supplied URL; private-IP guard is bypassable via DNS rebinding (TOCTOU) | `websearch.py` `fetch_page` / `pipelines/deliveries.py` |
| 2 | **Medium** | `X-Forwarded-For` trusted from anyone (`forwarded_allow_ips` defaults to `*`) → login throttle bypass, global login lockout (DoS), spoofed IPs in logs | `web/app.py` `run()` |
| 3 | Low | Pre-auth information disclosure (version, vault name, password/TOTP status) | `web/app.py` `session`, `healthz` |
| 4 | Low | Live Google OAuth refresh+access token in cleartext in the working tree | `.jarvis/google-token.json` |
| 5 | Low | Internal exception type/message returned to clients | `web/app.py` Guard, `chat` |
| 6 | Low | In-memory login throttle: global cap enables easy login DoS; no persistence | `auth.py` `throttled` |
| 7 | Info | Second factor (TOTP) is optional on an internet-exposed admin surface | `auth.py` / `login` |
| 8 | Info | HSTS / TLS configuration not verifiable from here — confirm externally | SWAG / nginx |
| 9 | Info | Push-notification link uses an attacker-influenceable tracking URL (mild phishing vector) | `pipelines/deliveries.py` `flush_notifications` |

---

## 3. Detailed findings

### 1 — Blind SSRF via email-sourced tracking URL + DNS-rebinding TOCTOU — *Medium*

**Where:** `src/jarvis/websearch.py` (`_public_address`, `fetch_page`); triggered from `src/jarvis/pipelines/deliveries.py` (`on_message` → `check`, line ~650).

**What happens.** Incoming email is parsed into a "delivery" automatically, with no confirmation step (`on_message`). If the email yields a tracking URL, polling is enabled on creation (`poll = int(bool(tracking_url))`), and the hourly job then fetches that URL server-side:

```python
# deliveries.py ~650
final, markup = await fetch_page(row["tracking_url"], self.settings.web_allow_private, timeout=15)
```

The URL comes from the email's HTML/body (`tracking_link(...)`, or a JSON-LD `tracking_url`). So an attacker who can get an email into Chris's primary inbox that looks like a dispatch notice (not SPAM, not `CATEGORY_PROMOTIONS`, matching the delivery regex) can choose the URL the server fetches, hourly.

`fetch_page` *does* defend against the obvious version of this — `_public_address` resolves the host and refuses private/loopback/link-local/CGNAT/multicast addresses, handles IPv4-mapped IPv6, and re-checks on every redirect hop. That correctly blocks `http://192.168.1.20:8123` (Home Assistant), the router, Nextcloud, Ollama, etc.

**The gap (TOCTOU / DNS rebinding).** `_public_address` resolves the name and validates the IPs, then `client.stream("GET", url)` resolves the name *again* when it connects. Between those two resolutions the DNS answer can change. An attacker who controls a domain can return a public IP for the check and a private IP (e.g. `192.168.1.20`) for the connect, defeating the guard. The result is **blind** SSRF (the fetched text feeds delivery-status parsing, not a response back to the attacker), but it reaches the internal network from an internet-delivered email.

**Impact.** Blind requests to arbitrary internal hosts/ports on the home LAN from the server's position, hourly, attacker-chosen. Severity is held to Medium because it is blind, requires a rebinding setup, and the naive direct-IP case is already blocked.

**Fix.** Resolve the host once, then connect to that *validated* IP and carry the hostname for TLS/SNI and the `Host` header — so the IP that was vetted is the IP that is used. Practically, with httpx you can resolve, re-check, and pass the literal IP while preserving the Host header, or use a custom `transport`/resolver that pins the checked address. Also consider: an allowlist of expected carrier domains for delivery polling, a dedicated egress with no route to RFC1918, and keeping `WEB_ALLOW_PRIVATE=false` (it is). Apply the same pinning to the web-research fetch path, which shares `fetch_page`.

---

### 2 — `X-Forwarded-For` trusted from any source — *Medium*

**Where:** `src/jarvis/web/app.py`:

```python
uvicorn.run(create_app(), host="0.0.0.0", port=8080, proxy_headers=True,
            forwarded_allow_ips=os.environ.get("JARVIS_TRUSTED_PROXIES", "*"), ...)
```

The default is `*` — uvicorn will trust `X-Forwarded-For`/`X-Forwarded-*` from *any* client, so the "client IP" the app sees (`client_ip()` → `request.client.host`) is ultimately influenced by a header the remote caller can set. Your own `deploy/.env.server.example` even documents the fix (`JARVIS_TRUSTED_PROXIES=172.18.0.0/16`) but leaves it commented out, so the permissive default is what runs.

**Impact.**

- **Login throttle bypass.** `auth.throttled()` buckets failures per `client_ip`. Rotating the forged `X-Forwarded-For` gives a fresh per-IP bucket each time, defeating the `>= 5 failures / 15 min` per-IP limit.
- **Login lockout DoS (see also #6).** The *global* limit `total >= 30` failures/hour blocks **all** logins, including the real user. With spoofable IPs an attacker can trivially drive 30 failures and lock Chris out of his own assistant for an hour, repeatedly.
- **Log/forensic spoofing.** Every `client_ip` in the diagnostics logs becomes attacker-controlled, undermining any later investigation.

**Fix.** Set `JARVIS_TRUSTED_PROXIES` to the SWAG container IP or the Docker network subnet (e.g. `172.18.0.0/16`) so only the real reverse proxy is trusted. Confirm which hop SWAG's `proxy.conf` places the genuine client IP at, and that uvicorn then derives it from the trusted proxy rather than the client-supplied portion of the header.

---

### 3 — Pre-authentication information disclosure — *Low*

**Where:** `web/app.py` `session()` and `healthz()` (both in `PUBLIC_PATHS`).

Confirmed live, unauthenticated:

```
GET /healthz        → ok 0.11.0
GET /api/session    → {"authenticated":false,"password_set":true,"totp":true,
                       "version":"0.11.0","vault":"Jarvis","tts":"kokoro"}
```

This hands an attacker the exact version (for targeting a known-version bug), confirms a password is set and TOTP is on, and leaks the vault name and TTS provider. None of it is catastrophic, but none of it needs to be public.

**Fix.** Drop the version from `/healthz` (a bare `ok` still satisfies the deploy check if you compare against something non-public, or gate the version behind auth). In `/api/session`, return only `{"authenticated": false}` when unauthenticated and move `version`/`vault`/`tts`/`password_set`/`totp` behind a valid session.

---

### 4 — Google OAuth token in cleartext in the working tree — *Low (hygiene)*

**Where:** `.jarvis/google-token.json` — a real `access_token` **and** `refresh_token` with Gmail/Calendar/Drive/Contacts scopes, in cleartext.

**Good news:** `.jarvis/` is in both `.gitignore` and `.dockerignore`, so this is **not** in version control and **not** baked into the image. At runtime the token lives at `/data/google-token.json` and is written `chmod 600` by `google/oauth.py`. So this is a dev-machine hygiene issue, not a shipped exposure.

**Why it still matters:** it is a live (or very recently live) credential sitting in plaintext in a folder that just got shared out of the machine. Anyone who reads that folder can impersonate the Google account until the token is revoked.

**Fix.**
1. Revoke/rotate it out of caution at `myaccount.google.com/permissions`, then reconnect via the app. (I did **not** use the token.)
2. Confirm it was never committed before the ignore rule existed: `git log --all --full-history -- .jarvis/google-token.json`. If it ever was, treat it as compromised and purge history.
3. Avoid keeping the prod token in the dev tree at all; let each environment mint its own.

---

### 5 — Internal error detail leaked to clients — *Low*

**Where:** `web/app.py` Guard returns `"Internal error ({type(exc).__name__})…"` on 500s, and `chat()` streams `f"(Error: {type(error).__name__}: {error} …)"` into the reply.

Exposes Python exception types and messages (and, in `chat`, the stringified exception) to the caller. Most of these paths require a valid session, so the audience is limited, but it still narrows the stack for anyone who gets that far and can surface internal paths/values in messages.

**Fix.** Return an opaque error + trace id to the client (you already generate a trace id — lean on it) and keep the type/message server-side in the logs only.

---

### 6 — In-memory login throttle with a global cap — *Low*

**Where:** `auth.py` `throttled()` / `Auth.__init__` (`self._failures` is a process dict).

Two issues: (a) state is per-process and in-memory, so it resets on restart and wouldn't be shared across workers if you ever scale; (b) the global `total >= 30` failures/hour cap locks out *everyone*, so it doubles as a denial-of-service lever against the legitimate user (amplified by #2's IP spoofing).

**Fix.** Keep the per-IP limit but reconsider the global one — rather than a hard global lockout, prefer escalating per-source delays, or a global cap that only slows rather than fully blocks, so an attacker can't lock the owner out. Persisting throttle/lockout state (you already have SQLite) would also survive restarts.

---

### 7 — Second factor is optional — *Info / hardening*

TOTP is supported and nicely implemented (replay-protected via `totp_last_counter`, ±1 step window), but login succeeds on password alone when TOTP isn't enabled. For a single-user admin surface exposed to the internet, consider making 2FA effectively mandatory (you already enforce a 12-char minimum password, which is good). You have it enabled (`"totp":true`), so this is really a "keep it that way / enforce it in code" note.

---

### 8 — TLS / HSTS not verifiable from here — *Info (unverified)*

The app sets strong response headers itself (`Content-Security-Policy` with no `unsafe-inline`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store` on API). HSTS and the TLS version/cipher/cert are terminated at SWAG and weren't observable through this environment's egress proxy.

**Confirm externally:** run the host through SSL Labs (`ssllabs.com/ssltest`) or `testssl.sh`, and check for `Strict-Transport-Security` (ideally with `includeSubDomains; preload`), TLS 1.2+/1.3 only, and that `jarvis.*` isn't inadvertently serving other vhosts. SWAG's defaults are usually sound, so this is a verify-not-assume item. `client_max_body_size 5m` in the proxy conf is a reasonable request-size cap.

---

### 9 — Notification link uses an attacker-influenceable URL — *Info*

**Where:** `pipelines/deliveries.py` `flush_notifications()` sends an ntfy push with `url=item["tracking_url"]`. Since a spoofed delivery email (#1) controls that URL, a fake dispatch notice could produce a push notification whose "track" link points wherever the attacker likes — a low-effort phishing nudge. Minor, and bounded by the same "email must parse as a delivery" constraint.

**Fix.** Constrain notification link hosts to known carriers, or show the carrier name without making the raw email-derived URL the tappable target.

---

## 4. What's done well (so you don't regress it)

- **SQL:** every query is parameterised; `LIKE` uses `ESCAPE`; the one dynamic JSON field in `notes_where` is validated against `[\w-]+`. No injection found.
- **Path traversal:** `FileVault._full()` normalises separators, rejects `..`/empty/`~`/absolute, and re-checks the resolved path is inside the vault root. `vault_note` adds a second dotfile/`..` screen.
- **SSRF base case:** `_public_address` is a genuinely good allow-by-default-deny-private check (covers IPv4-mapped v6 and CGNAT) and redirects are re-validated each hop — see #1 only for the rebinding refinement.
- **CSRF:** custom `x-jarvis: 1` header (not settable cross-origin without preflight) + Origin check + `SameSite=strict` cookie.
- **Sessions:** 256-bit tokens, stored as SHA-256 hashes, sliding 30-day expiry, `HttpOnly` + `Secure` + `SameSite=strict`.
- **Passwords:** scrypt (N=2¹⁵), constant-time compare, 12-char minimum. TOTP replay-protected.
- **State changes gated:** Home Assistant control is regex-parsed (no LLM in the loop), unlock/alarm-disarm are hard-blocked, and actions require explicit confirmation. Calendar add defaults to "always ask". Web/email content is consistently framed to the model as untrusted data.
- **Secrets & supply chain:** `.env*`, `.jarvis/`, certs, releases all git- and docker-ignored; token written `chmod 600`.
- **Container:** `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, vault mounted `:ro`, `tmpfs` for `/tmp`, SearXNG not exposed through SWAG.

---

## 5. Recommended order of work

1. **#2** — set `JARVIS_TRUSTED_PROXIES` to the Docker subnet. One line of config; removes throttle bypass, the login-lockout DoS, and log spoofing.
2. **#1** — pin the resolved IP in `fetch_page` (and/or allowlist carrier domains for delivery polling). Closes the one remotely-reachable SSRF path.
3. **#4** — rotate the Google token and check git history.
4. **#3, #5** — trim pre-auth disclosure and opaque-ify client errors.
5. **#6, #7, #8, #9** — throttle redesign, enforce 2FA, external TLS/HSTS check, constrain notification link hosts.

---

## 6. Caveats

This is static reasoning plus benign recon, not a live exploitation test, so the two Medium items are **assessed, not demonstrated** — I did not fire a rebinding payload or spoof headers against your host. If you want any of them confirmed empirically on infrastructure you own, that's a reasonable next step with dedicated tooling; I can help you design safe, scoped test cases (e.g. a local rebinding harness against a disposable instance) rather than run live exploits here.
