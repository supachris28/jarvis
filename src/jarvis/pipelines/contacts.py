"""Tier 0 Contacts pipeline: seed and maintain People notes from Google Contacts (no LLM)."""

from __future__ import annotations

import json

from ..db import Database
from ..google.contacts import Contacts
from ..vault.markdown import safe_name
from ..vault.writer import VaultWriter


class ContactsPipeline:
    def __init__(self, db: Database, contacts: Contacts, writer: VaultWriter) -> None:
        self.db = db
        self.contacts = contacts
        self.writer = writer

    async def run(self) -> dict:
        seen = changed = 0
        for contact in await self.contacts.all():
            seen += 1
            if self.upsert(contact):
                changed += 1
        return {"contacts": seen, "changed": changed}

    def path_for(self, contact: dict) -> str:
        for email in contact["emails"]:
            path = self.writer.person_path(email)
            if path:
                return path
        row = self.db.one("SELECT path FROM contacts WHERE resource_name = ?", (contact["resource_name"],))
        if row:
            return row["path"]
        return f"People/{safe_name(contact['name'])}.md"

    def upsert(self, contact: dict) -> bool:
        existing = self.db.one("SELECT etag, path FROM contacts WHERE resource_name = ?", (contact["resource_name"],))
        path = self.path_for(contact)
        for email in contact["emails"]:
            self.db.execute(
                "INSERT INTO people (email, name, path) VALUES (?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET path = excluded.path",
                (email, contact["name"], path),
            )
        if existing is not None and existing["etag"] == contact["etag"] and existing["path"] == path:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO contacts (resource_name, etag, name, path, data, birthday) VALUES (?, ?, ?, ?, ?, ?)",
            (contact["resource_name"], contact["etag"], contact["name"], path, json.dumps(contact),
             contact["birthday"]),
        )
        self.db.queue_note("contact", contact["resource_name"])
        for email in contact["emails"][:1]:
            self.db.queue_note("person", email)
        return True
