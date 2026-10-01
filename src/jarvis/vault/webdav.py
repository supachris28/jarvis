"""Write vault notes through Nextcloud's WebDAV API.

Jarvis reads the vault straight from disk (fast, works for the index), but writes go through
Nextcloud so that Nextcloud knows about every change immediately: your desktop and phone
clients sync it right away, and Nextcloud keeps a version history of each note.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from .client import VaultError, VaultUnavailable


class NextcloudWriter:
    def __init__(self, url: str, user: str, app_password: str, folder: str, verify_tls: bool = True) -> None:
        self.base = f"{url.rstrip('/')}/remote.php/dav/files/{quote(user, safe='')}"
        self.folder = folder.strip("/")
        self.auth = (user, app_password)
        self.verify = verify_tls

    @property
    def configured(self) -> bool:
        return bool(self.auth[0] and self.auth[1])

    def _url(self, path: str) -> str:
        parts = [p for p in f"{self.folder}/{path}".split("/") if p]
        return self.base + "/" + "/".join(quote(p, safe="") for p in parts)

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await client.request(method, self._url(path), auth=self.auth, **kwargs)
        except httpx.HTTPError as error:
            raise VaultUnavailable(f"Nextcloud is not reachable ({type(error).__name__}).") from None

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=10, verify=self.verify) as client:
            try:
                response = await self._request(client, "PROPFIND", "", headers={"Depth": "0"})
            except VaultUnavailable as error:
                return {"ok": False, "detail": str(error)}
        if response.status_code == 401:
            return {"ok": False, "detail": "Nextcloud rejected the app password (NEXTCLOUD_APP_PASSWORD)"}
        if response.status_code == 404:
            return {"ok": False, "detail": f"folder '{self.folder}' not found in Nextcloud"}
        if response.status_code >= 400:
            return {"ok": False, "detail": f"Nextcloud WebDAV HTTP {response.status_code}"}
        return {"ok": True, "detail": "writes via Nextcloud"}

    async def _mkdirs(self, client: httpx.AsyncClient, path: str) -> None:
        parts = [p for p in path.split("/")[:-1] if p]
        for depth in range(1, len(parts) + 1):
            response = await self._request(client, "MKCOL", "/".join(parts[:depth]))
            if response.status_code not in (201, 405):  # 405 = already exists
                hint = ""
                if response.status_code in (403, 404, 409):
                    hint = " — check NEXTCLOUD_USER (the user ID, case-sensitive) and NEXTCLOUD_VAULT_DIR"
                raise VaultError(f"Nextcloud could not create folder {'/'.join(parts[:depth])} "
                                 f"(HTTP {response.status_code}){hint}")

    async def put(self, path: str, text: str) -> None:
        body = text.encode("utf-8")
        headers = {"Content-Type": "text/markdown; charset=utf-8"}
        async with httpx.AsyncClient(timeout=30, verify=self.verify) as client:
            for attempt in range(3):
                response = await self._request(client, "PUT", path, content=body, headers=headers)
                if response.status_code in (200, 201, 204):
                    return
                if response.status_code in (404, 409) and attempt == 0:
                    # parent folder missing — Nextcloud answers 404 ("could not be located"), plain Sabre 409
                    await self._mkdirs(client, path)
                    continue
                if response.status_code == 404:
                    raise VaultError(f"Nextcloud says the folder for {path} doesn't exist even after creating it — "
                                     f"check NEXTCLOUD_USER (the user ID, case-sensitive) and NEXTCLOUD_VAULT_DIR.")
                if response.status_code == 423 and attempt < 2:  # file locked by a sync client
                    await asyncio.sleep(1.5)
                    continue
                if response.status_code == 401:
                    raise VaultError("Nextcloud rejected the app password (NEXTCLOUD_APP_PASSWORD).")
                hint = " — check NEXTCLOUD_USER (the user ID, case-sensitive)" if response.status_code == 403 else ""
                raise VaultError(f"Nextcloud refused to save {path} (HTTP {response.status_code}){hint}.")
        raise VaultError(f"Nextcloud could not save {path}.")
