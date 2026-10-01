"""Client for the Obsidian Local REST API plugin running on the PC.

Uses the plugin's REST endpoints, which give Jarvis fast access to Obsidian's own
metadata cache: tags, frontmatter (JsonLogic search), full-text search and, when the
Dataview plugin is installed, link relationships (backlinks) through DQL.
"""

from __future__ import annotations

import re
import ssl
from urllib.parse import quote

import httpx

from .. import diag

NOTE_JSON = "application/vnd.olrapi.note+json"
JSONLOGIC = "application/vnd.olrapi.jsonlogic+json"
DQL = "application/vnd.olrapi.dataview.dql+txt"

WIKILINK = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")


class VaultError(Exception):
    """A user-facing vault error."""


class VaultUnavailable(VaultError):
    """The vault (Obsidian on the PC) cannot be reached right now."""


def encode_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.strip("/").split("/"))


def note_title(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def outlinks(content: str) -> list[str]:
    seen: list[str] = []
    for match in WIKILINK.finditer(content):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


class ObsidianVault:
    def __init__(self, base_url: str, api_key: str, ca_cert: str = "", verify_tls: bool = True,
                 timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        if ca_cert:
            self.verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=ca_cert)
        else:
            self.verify = verify_tls

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _request(self, method: str, path: str, *, headers: dict | None = None,
                       content: str | bytes | None = None, params: dict | None = None) -> httpx.Response:
        if not self.configured:
            raise VaultUnavailable("OBSIDIAN_API_KEY is not set.")
        all_headers = {"Authorization": f"Bearer {self.api_key}"}
        all_headers.update(headers or {})
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify) as client:
                response = await client.request(method, self.base_url + path, headers=all_headers,
                                                content=content, params=params)
        except httpx.TransportError as error:
            raise VaultUnavailable(f"Obsidian is not reachable ({type(error).__name__}).") from None
        if response.status_code in (401, 403):
            raise VaultError("Obsidian rejected the API key (check OBSIDIAN_API_KEY).")
        return response

    @staticmethod
    def _check(response: httpx.Response, action: str) -> None:
        if response.status_code >= 400:
            detail = response.text.strip()[:300]
            raise VaultError(f"Obsidian {action} failed (HTTP {response.status_code}): {detail}")

    async def health(self) -> dict:
        try:
            response = await self._request("GET", "/")
            data = response.json()
        except VaultError as error:
            return {"ok": False, "detail": str(error)}
        except ValueError:
            return {"ok": False, "detail": "unexpected response"}
        if not data.get("authenticated"):
            return {"ok": False, "detail": "not authenticated"}
        return {"ok": True, "detail": f"ready ({data.get('service', 'Obsidian Local REST API')})"}

    # files ---------------------------------------------------------------
    async def get_note(self, path: str) -> dict | None:
        """Return {content, frontmatter, tags, path, stat} or None if missing."""
        with diag.expected_errors():  # a missing note is normal
            response = await self._request("GET", f"/vault/{encode_path(path)}", headers={"Accept": NOTE_JSON})
        if response.status_code == 404:
            return None
        self._check(response, f"read of {path}")
        return response.json()

    async def get_text(self, path: str) -> str | None:
        note = await self.get_note(path)
        return None if note is None else str(note.get("content", ""))

    async def put_text(self, path: str, text: str) -> None:
        response = await self._request("PUT", f"/vault/{encode_path(path)}",
                                       headers={"Content-Type": "text/markdown; charset=utf-8"},
                                       content=text.encode("utf-8"))
        self._check(response, f"write of {path}")

    async def list_dir(self, directory: str = "") -> list[str]:
        path = "/vault/" + (encode_path(directory) + "/" if directory.strip("/") else "")
        with diag.expected_errors():
            response = await self._request("GET", path)
        if response.status_code == 404:
            return []
        self._check(response, f"listing of {directory or '/'}")
        return [str(item) for item in response.json().get("files", [])]

    # search / discovery -------------------------------------------------
    async def search_simple(self, query: str, context_length: int = 120) -> list[dict]:
        response = await self._request("POST", "/search/simple/",
                                       params={"query": query, "contextLength": context_length})
        self._check(response, "search")
        results = response.json()
        return results if isinstance(results, list) else []

    async def search_jsonlogic(self, expression: dict) -> list[dict]:
        import json

        response = await self._request("POST", "/search/", headers={"Content-Type": JSONLOGIC},
                                       content=json.dumps(expression))
        self._check(response, "structured search")
        results = response.json()
        return results if isinstance(results, list) else []

    async def search_dql(self, query: str) -> list[dict]:
        response = await self._request("POST", "/search/", headers={"Content-Type": DQL},
                                       content=query.encode("utf-8"))
        self._check(response, "Dataview query")
        results = response.json()
        return results if isinstance(results, list) else []

    async def notes_with_tag(self, tag: str) -> list[str]:
        tag = tag.lstrip("#")
        expression = {"or": [
            {"in": [tag, {"var": "tags"}]},
            {"in": ["#" + tag, {"var": "tags"}]},
        ]}
        return [str(r.get("filename")) for r in await self.search_jsonlogic(expression) if r.get("filename")]

    async def notes_where(self, field: str, value: str) -> list[str]:
        expression = {"or": [
            {"==": [{"var": f"frontmatter.{field}"}, value]},
            {"in": [value, {"var": f"frontmatter.{field}"}]},
        ]}
        return [str(r.get("filename")) for r in await self.search_jsonlogic(expression) if r.get("filename")]

    async def backlinks(self, path: str) -> list[str]:
        """Notes that link to `path`. Uses Dataview when present, else a text search."""
        title = note_title(path).replace('"', "")
        try:
            with diag.expected_errors():
                rows = await self.search_dql(f'TABLE file.mtime FROM [[{title}]]')
            return [str(r.get("filename")) for r in rows if r.get("filename") and r.get("filename") != path]
        except VaultError as error:
            diag.debug("obsidian", "Dataview not available — using text search for backlinks", error=str(error)[:200])
            rows = await self.search_simple(f"[[{title}", context_length=10)
            return [str(r.get("filename")) for r in rows if r.get("filename") and r.get("filename") != path]
