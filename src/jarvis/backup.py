"""Nightly backup of Jarvis's own database (deliveries, reminders, chat, settings, history, reports).

The vault is already safe in Nextcloud; this database only exists on the server. Each night a consistent copy is
taken with SQLite's online backup (no need to stop Jarvis), scrubbed of sign-in secrets, compressed, kept in
/data/backups (last 7) and — when Nextcloud is set up — uploaded to a separate Nextcloud folder (BACKUP_DIR,
never inside the vault), rotating by weekday plus one per month.

Restoring: stop Jarvis, gunzip a copy to /data/jarvis.sqlite3, start it, then sign in with your password (2FA and
open sessions are not in backups, so set up 2FA again) and reconnect Google if asked.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from . import diag
from .config import Settings
from .db import Database
from .vault.client import VaultError
from .vault.webdav import NextcloudWriter

KEEP_LOCAL = 7


class Backup:
    def __init__(self, settings: Settings, db: Database, uploader: NextcloudWriter | None = None) -> None:
        self.settings = settings
        self.db = db
        self.uploader = uploader

    @property
    def folder(self) -> Path:
        return self.settings.data_dir / "backups"

    def due(self) -> bool:
        spec = (self.settings.backup_time or "").strip().casefold()
        if spec in ("", "off", "none"):
            return False
        now = datetime.now(self.settings.tz)
        try:
            hour, minute = (int(x) for x in spec.split(":"))
        except ValueError:
            hour, minute = 3, 15
        return (now.hour, now.minute) >= (hour, minute) and not self.db.get(f"backup.done.{now:%Y-%m-%d}")

    def snapshot(self) -> bytes:
        """A gzip of a consistent copy of the database, without sign-in secrets."""
        with tempfile.TemporaryDirectory() as tmp:
            copy_path = Path(tmp) / "jarvis.sqlite3"
            copy = sqlite3.connect(copy_path)
            try:
                with self.db._lock:
                    self.db._conn.backup(copy)
                copy.execute("DELETE FROM sessions")
                copy.execute("UPDATE auth SET totp_secret = NULL, totp_enabled = 0, totp_last_counter = 0")
                copy.commit()
                copy.execute("VACUUM")
            finally:
                copy.close()
            return gzip.compress(copy_path.read_bytes(), compresslevel=6)

    def _keep_locally(self, data: bytes, day: str) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        path = self.folder / f"jarvis-{day}.sqlite3.gz"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        for old in sorted(self.folder.glob("jarvis-*.sqlite3.gz"))[:-KEEP_LOCAL]:
            old.unlink(missing_ok=True)
        return path

    async def run(self, force: bool = False) -> str:
        if not force and not self.due():
            return "not due"
        now = datetime.now(self.settings.tz)
        data = await asyncio.to_thread(self.snapshot)
        path = await asyncio.to_thread(self._keep_locally, data, f"{now:%Y-%m-%d}")
        size = f"{len(data) / 1_048_576:.1f} MB"
        where = [f"kept in {path.parent} (last {KEEP_LOCAL})"]
        if self.uploader is not None and self.uploader.configured:
            names = [f"jarvis-{now:%a}.sqlite3.gz"] + ([f"jarvis-month-{now:%m}.sqlite3.gz"] if now.day == 1 else [])
            try:
                for name in names:
                    await self.uploader.put_bytes(name, data, "application/gzip")
                where.append(f"uploaded to Nextcloud {self.uploader.folder}/{names[0]}")
            except VaultError as error:
                diag.warning("backup", f"Nextcloud upload failed: {error}")
                where.append(f"Nextcloud upload failed: {error}")
                self.db.set(f"backup.done.{now:%Y-%m-%d}", True)
                return f"{size} — " + "; ".join(where)
        self.db.set(f"backup.done.{now:%Y-%m-%d}", True)
        self.db.set("backup.last", {"ts": now.timestamp(), "size": len(data), "where": where})
        diag.event("backup", f"database backed up ({size})", where=where)
        return f"{size} — " + "; ".join(where)
