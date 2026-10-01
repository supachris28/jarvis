"""Google OAuth (web application client) with a stored refresh token."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .. import diag

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",   # read + add events (always after your confirmation)
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
)


class GoogleError(Exception):
    """A user-facing Google error."""


class GoogleAuthRequired(GoogleError):
    """No usable Google token; the user must connect Google in the web UI."""


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, token_path: Path,
                 scopes: tuple[str, ...] = SCOPES) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self.scopes = scopes
        self._token: dict | None = None
        self._lock = asyncio.Lock()
        self._pending_states: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        token = self._load()
        return bool(token and token.get("refresh_token"))

    def status(self) -> dict:
        if not self.configured:
            return {"ok": False, "detail": "not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)"}
        token = self._load()
        if not token or not token.get("refresh_token"):
            return {"ok": False, "detail": "not connected — use Connect Google"}
        if token.get("error"):
            return {"ok": False, "detail": f"re-auth needed: {token['error']}"}
        missing = self.missing_scopes()
        if missing:
            names = ", ".join(m.rsplit("/", 1)[-1] for m in missing)
            return {"ok": False, "detail": f"reconnect Google to grant: {names}"}
        return {"ok": True, "detail": "connected"}

    def missing_scopes(self) -> list[str]:
        token = self._load() or {}
        granted = set(str(token.get("scope", "")).split())
        if not granted:
            return []
        return [s for s in self.scopes if s not in granted]

    def has_scope(self, scope: str) -> bool:
        token = self._load() or {}
        granted = set(str(token.get("scope", "")).split())
        return not granted or scope in granted

    def _load(self) -> dict | None:
        if self._token is not None:
            return self._token
        try:
            self._token = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None
        return self._token

    def _save(self, token: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_path)
        self._token = token

    # authorization code flow ------------------------------------------------
    def authorization_url(self) -> str:
        if not self.configured:
            raise GoogleError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.")
        now = time.time()
        self._pending_states = {s: t for s, t in self._pending_states.items() if now - t < 600}
        state = secrets.token_urlsafe(24)
        self._pending_states[state] = now
        query = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    async def complete(self, code: str, state: str) -> None:
        if self._pending_states.pop(state, None) is None:
            raise GoogleError("Google sign-in expired or the state did not match. Try again.")
        token = await self._token_request({
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        })
        if not token.get("refresh_token"):
            previous = self._load() or {}
            if previous.get("refresh_token"):
                token["refresh_token"] = previous["refresh_token"]
            else:
                raise GoogleError("Google did not return a refresh token. Remove Jarvis access at "
                                  "myaccount.google.com/permissions and connect again.")
        token["obtained_at"] = int(time.time())
        self._save(token)

    async def _token_request(self, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(TOKEN_URL, data=payload)
                data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise GoogleError(f"Could not reach Google OAuth: {type(error).__name__}") from None
        if response.status_code >= 400 or "error" in data:
            raise GoogleError(str(data.get("error_description") or data.get("error") or response.status_code))
        return data

    async def access_token(self) -> str:
        async with self._lock:
            token = self._load()
            if not token or not token.get("refresh_token"):
                raise GoogleAuthRequired("Google is not connected. Open Jarvis → Status → Connect Google.")
            expires_at = int(token.get("obtained_at", 0)) + int(token.get("expires_in", 0)) - 120
            if token.get("access_token") and expires_at > time.time() and not token.get("error"):
                return str(token["access_token"])
            try:
                refreshed = await self._token_request({
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": token["refresh_token"],
                    "grant_type": "refresh_token",
                })
            except GoogleError as error:
                if "invalid_grant" in str(error) or "expired" in str(error).lower() or "revoked" in str(error).lower():
                    token["error"] = str(error)
                    self._save(token)
                    diag.error("google", f"refresh token rejected — reconnect Google: {error}")
                    raise GoogleAuthRequired(f"Google access expired or was revoked: {error}") from None
                raise
            diag.event("google", "access token refreshed", expires_in=refreshed.get("expires_in"),
                       scopes=str(refreshed.get("scope", "")).split())
            refreshed["refresh_token"] = token["refresh_token"]
            refreshed["obtained_at"] = int(time.time())
            refreshed.pop("error", None)
            self._save(refreshed)
            return str(refreshed["access_token"])

    async def get(self, url: str, params: dict | list | None = None) -> dict:
        return await self.request("GET", url, params=params)

    async def post(self, url: str, body: dict, params: dict | None = None) -> dict:
        return await self.request("POST", url, params=params, json_body=body)

    async def request(self, method: str, url: str, params: dict | list | None = None,
                      json_body: dict | None = None) -> dict:
        token = await self.access_token()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.request(method, url, params=params, json=json_body,
                                                headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as error:
            raise GoogleError(f"Could not reach Google: {type(error).__name__}") from None
        if response.status_code == 401:
            self._token = None
            raise GoogleAuthRequired("Google rejected the token; reconnect Google.")
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = response.text[:200]
            error = GoogleError(f"Google API HTTP {response.status_code}: {message}")
            error.status = response.status_code  # type: ignore[attr-defined]
            raise error
        return response.json()
