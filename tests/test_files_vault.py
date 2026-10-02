"""File-based vault (folder shared with Nextcloud) and the Nextcloud WebDAV writer."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from fakes import Server
from jarvis.db import Database
from jarvis.vault.client import VaultError
from jarvis.vault.files import FileVault, extract_links, extract_tags
from jarvis.vault.webdav import NextcloudWriter
from test_jarvis import FakeGmail, IntegrationBase, gmail_message


class FakeNextcloud:
    """Minimal WebDAV: PUT (409 if parent missing), MKCOL, PROPFIND — files land in `root`."""

    def __init__(self, root: Path, user: str = "chris", password: str = "app-pass", folder: str = "Obsidian/Jarvis"):
        self.root, self.user, self.password, self.folder = root, user, password, folder
        self.puts: list[str] = []
        self.missing_parent_status = 404  # what Nextcloud really returns; plain SabreDAV uses 409

    def app(self) -> Starlette:
        import base64

        async def dav(request: Request):
            auth = request.headers.get("authorization", "")
            if auth != "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode():
                return PlainTextResponse("no", status_code=401)
            path = unquote(request.path_params["path"])
            prefix = f"{self.user}/{self.folder}"
            if not path.startswith(prefix):
                return PlainTextResponse("outside", status_code=403)
            target = self.root / path[len(prefix):].strip("/")
            if request.method == "PROPFIND":
                return Response(status_code=207 if target.exists() else 404)
            if request.method == "MKCOL":
                if target.exists():
                    return Response(status_code=405)
                if not target.parent.exists():
                    return Response(status_code=409)
                target.mkdir()
                return Response(status_code=201)
            if request.method == "PUT":
                if not target.parent.exists():
                    return Response(b'<?xml version="1.0"?><d:error xmlns:d="DAV:"><s:message xmlns:s="http://sabredav.org/ns">'
                                    b'File with name //Sources could not be located</s:message></d:error>',
                                    status_code=self.missing_parent_status, media_type="application/xml")
                existed = target.exists()
                target.write_bytes(await request.body())
                self.puts.append(path[len(prefix):].strip("/"))
                return Response(status_code=204 if existed else 201)
            return Response(status_code=405)

        return Starlette(routes=[Route("/remote.php/dav/files/{path:path}", dav,
                                       methods=["PUT", "MKCOL", "PROPFIND"])])


def run(coro):
    return asyncio.run(coro)


class FileVaultUnitTests(IntegrationBase.__mro__[1]):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = FileVault(self.root, Database(":memory:"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parsing(self):
        self.assertEqual(extract_tags({"tags": ["person", "#friend"]}, "Loves #climbing and #family/kids `#notatag`"),
                         ["person", "friend", "climbing", "family", "family/kids"])
        self.assertEqual(extract_links("See [[People/Sam Jones|Sam]] and [[Hiking#Plan]] and [x](Notes/Trip%20Ideas.md)"),
                         [("sam jones", "people/sam jones"), ("hiking", ""), ("trip ideas", "notes/trip ideas")])

    def test_read_write_index_and_search(self):
        run(self.vault.put_text("People/Sam Jones.md", "---\nemails: [sam@example.com]\ntags: [person]\n---\n# Sam\nClimber. #friend\n"))
        run(self.vault.put_text("Notes/Hiking.md", "# Hiking\nTrip with [[Sam Jones]] to the Lakes. #outdoors\n"))
        run(self.vault.put_text("Notes/Other.md", "# Other\nNothing about walking here.\n"))
        note = run(self.vault.get_note("People/Sam Jones.md"))
        self.assertEqual(note["frontmatter"]["emails"], ["sam@example.com"])
        self.assertIn("friend", note["tags"])
        self.assertEqual(run(self.vault.notes_with_tag("#outdoors")), ["Notes/Hiking.md"])
        self.assertEqual(run(self.vault.notes_where("emails", "SAM@example.com")), ["People/Sam Jones.md"])
        self.assertEqual(run(self.vault.backlinks("People/Sam Jones.md")), ["Notes/Hiking.md"])
        hits = run(self.vault.search_simple("trip lakes"))
        self.assertEqual(hits[0]["filename"], "Notes/Hiking.md")
        self.assertEqual(run(self.vault.list_dir("")), ["Notes/", "People/"])
        self.assertIsNone(run(self.vault.get_note("Missing.md")))
        self.assertTrue(run(self.vault.health())["ok"])
        # no temp files left behind, and files are group-writable for Nextcloud
        self.assertEqual(sorted(p.name for p in (self.root / "Notes").iterdir()), ["Hiking.md", "Other.md"])
        self.assertEqual(oct((self.root / "Notes/Hiking.md").stat().st_mode & 0o777), "0o664")

    def test_external_edits_and_safety(self):
        run(self.vault.put_text("A.md", "# A\n"))
        (self.root / "B.md").write_text("# B\nlinks to [[A]] #new\n")          # edited via Nextcloud
        (self.root / ".obsidian").mkdir()
        (self.root / ".obsidian" / "x.md").write_text("ignored")
        (self.root / "C.md.jarvis~").write_text("ignored")
        result = run(self.vault.refresh())
        self.assertEqual((result["notes"], result["changed"]), (2, 1))
        self.assertEqual(run(self.vault.backlinks("A.md")), ["B.md"])
        time.sleep(0.01)
        (self.root / "B.md").write_text("# B\nno links now\n")
        os.utime(self.root / "B.md", None)
        run(self.vault.refresh())
        self.assertEqual(run(self.vault.backlinks("A.md")), [])
        (self.root / "B.md").unlink()
        self.assertEqual(run(self.vault.refresh())["removed"], 1)
        for bad in ("../escape.md", "/etc/passwd", "a/../../b.md", ""):
            with self.assertRaises(VaultError):
                run(self.vault.put_text(bad, "x"))


class FilesBackendIntegration(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.vault_dir = Path(self.tmp.name) / "vault"
        self.vault_dir.mkdir()
        (self.vault_dir / "People").mkdir()
        (self.vault_dir / "People" / "Sam Jones.md").write_text(
            "---\nemails: [sam@example.com]\nrelation: friend\ntags: [person]\n---\n# Sam Jones\n\nMet at uni.\n")
        (self.vault_dir / "Notes").mkdir()
        (self.vault_dir / "Notes" / "Hiking.md").write_text("# Hiking\nPlanning a trip with [[Sam Jones]] #outdoors\n")
        self.nextcloud = FakeNextcloud(self.vault_dir)
        self.nc_server = Server(self.nextcloud.app()).__enter__()
        writer = NextcloudWriter(self.nc_server.url, "chris", "app-pass", "Obsidian/Jarvis")
        vault = FileVault(self.vault_dir, self.services.db, writer=writer)
        s = self.services
        s.vault = s.writer.vault = s.assistant.vault = s.scheduler.vault = vault
        run(vault.refresh())

    def tearDown(self):
        self.nc_server.__exit__()
        super().tearDown()

    def test_pipeline_writes_via_nextcloud_and_chat_reads_files(self):
        s = self.services
        run(s.writer.refresh_people())
        s.gmail_pipeline.gmail = FakeGmail([gmail_message("m1", "t1", "Sam Jones <sam@example.com>", "House move",
                                                          "Moving to Leeds in November")])
        run(s.gmail_pipeline.run())
        result = run(s.writer.flush())
        self.assertGreaterEqual(result["written"], 3)
        self.assertTrue(any(p.startswith("Sources/Email/") for p in self.nextcloud.puts), self.nextcloud.puts)
        sam = (self.vault_dir / "People" / "Sam Jones.md").read_text()
        self.assertIn("Met at uni.", sam)
        self.assertIn("House move", sam)
        # the index saw the writes immediately (backlinks from the new thread note)
        backlinks = run(s.vault.backlinks("People/Sam Jones.md"))
        self.assertTrue(any(b.startswith("Sources/Email/") for b in backlinks), backlinks)
        # chat answers from the files backend
        async def ask(text):
            events = []
            async for e in s.assistant.handle(text):
                events.append(e)
            return events
        events = run(ask("What do my notes say about Sam?"))
        sources = next(e for e in events if e["type"] == "sources")["items"]
        self.assertEqual(sources[0]["path"], "People/Sam Jones.md")
        prompt = self.ollama.requests[-1]["messages"][-1]["content"]
        self.assertIn("Linked from:", prompt)
        events = run(ask("Show notes tagged #outdoors"))
        self.assertTrue(any(x["label"] == "Hiking" for x in next(e for e in events if e["type"] == "sources")["items"]))
        health = run(s.vault.health())
        self.assertTrue(health["ok"], health)
        self.assertIn("writes via Nextcloud", health["detail"])

    def test_nightly_backup(self):
        import gzip
        import sqlite3
        from jarvis.auth import Auth
        from jarvis.backup import Backup
        s = self.services
        Auth(s.db).set_password("a very long password")
        s.db.execute("UPDATE auth SET totp_secret = 'SECRET', totp_enabled = 1")
        s.db.execute("INSERT INTO sessions (token_hash, created, last_seen, expires) VALUES ('t', 1, 1, 9e9)")
        s.db.execute("INSERT INTO scheduled (created, kind, text, due, repeat, payload, status, source) "
                     "VALUES (1, 'reminder', 'call Sam', 2e9, '', '{}', 'scheduled', 'chat')")
        backup_root = Path(self.tmp.name) / "nextcloud-backups"
        backup_root.mkdir()
        cloud = FakeNextcloud(backup_root, folder="Backups/Jarvis")
        with Server(cloud.app()) as server:
            backup = Backup(self.settings, s.db, NextcloudWriter(server.url, "chris", "app-pass", "Backups/Jarvis"))
            self.settings.backup_time = "off"
            self.assertFalse(backup.due())
            self.assertEqual(run(backup.run()), "not due")
            result = run(backup.run(force=True))
        self.assertIn("uploaded to Nextcloud", result)
        local = sorted((self.settings.data_dir / "backups").glob("jarvis-*.sqlite3.gz"))
        self.assertEqual(len(local), 1)
        uploaded = [p for p in backup_root.iterdir() if p.name.startswith("jarvis-")]
        self.assertTrue(uploaded)
        restored = Path(self.tmp.name) / "restored.sqlite3"
        restored.write_bytes(gzip.decompress(uploaded[0].read_bytes()))
        db = sqlite3.connect(restored)
        self.assertEqual(db.execute("SELECT text FROM scheduled").fetchone()[0], "call Sam")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0, "no sign-in sessions")
        self.assertEqual(db.execute("SELECT totp_secret, totp_enabled FROM auth").fetchone(), (None, 0))
        self.assertTrue(db.execute("SELECT password_hash FROM auth").fetchone()[0])
        db.close()
        self.assertEqual(s.db.one("SELECT totp_secret FROM auth")["totp_secret"], "SECRET", "the live database is untouched")
        # a week of local copies at most
        for n in range(10):
            backup._keep_locally(b"x", f"2026-01-{n + 1:02d}")
        self.assertEqual(len(list((self.settings.data_dir / "backups").glob("jarvis-*.sqlite3.gz"))), 7)

    def test_notes_can_be_read_in_jarvis(self):
        from starlette.testclient import TestClient
        from jarvis.auth import Auth
        from jarvis.web.app import create_app
        (self.vault_dir / ".obsidian").mkdir()
        (self.vault_dir / ".obsidian" / "app.md").write_text("private")
        Auth(self.services.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(create_app(self.settings, self.services, start_jobs=False),
                        base_url="http://localhost:8080") as client:
            self.assertEqual(client.get("/api/vault/note?path=Notes/Hiking").status_code, 401)
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            for target in ("People/Sam Jones", "People/Sam Jones.md", "Sam Jones", "sam jones", "Sam Jones#Contact"):
                note = client.get("/api/vault/note", params={"path": target}, headers=h).json()
                self.assertEqual(note["path"], "People/Sam Jones.md", target)
            self.assertEqual(note["title"], "Sam Jones")
            self.assertEqual(note["properties"]["relation"], "friend")
            self.assertNotIn("emails:", note["body"])
            self.assertTrue(note["obsidian_url"].startswith("obsidian://open?"))
            for bad in ("Nowhere", "../etc/passwd", ".obsidian/app", "", "/"):
                self.assertEqual(client.get("/api/vault/note", params={"path": bad}, headers=h).status_code, 404, bad)

    def test_people_you_wrote_yourself_are_found_by_name(self):
        s = self.services
        s.settings.web_search_provider = "searxng"  # would search online if it didn't recognise her
        family = self.vault_dir / "People" / "Family"
        family.mkdir()
        (family / "Jennifer Key.md").write_text("---\naliases: [Jen]\n---\n# Jennifer Key\nChris's sister. Lives in York.\n")
        # lots of notes mention "key" — they mustn't crowd her out
        for n in range(12):
            (self.vault_dir / "Notes" / f"Keys {n}.md").write_text("key key key about the car key\n")
        run(s.vault.refresh())
        router_calls = len([r for r in self.ollama.requests if r.get("format") == "json"])

        async def ask(text):
            return [e async for e in s.assistant.handle(text)]
        for prompt in ("tell me about Jennifer Key", "what's new with jennifer key?", "How is Jen doing?"):
            events = run(ask(prompt))
            meta = next(e for e in events if e["type"] == "meta")
            self.assertEqual(meta["route"], "vault", prompt)
            sources = next(e for e in events if e["type"] == "sources")["items"]
            self.assertEqual(sources[0]["path"], "People/Family/Jennifer Key.md", prompt)
            self.assertIn("Lives in York", self.ollama.requests[-1]["messages"][-1]["content"])
        # recognised by script, so the router model wasn't needed
        self.assertEqual(len([r for r in self.ollama.requests if r.get("format") == "json"]), router_calls)
        # full-text search ranks the name as a phrase above notes that just say "key"
        hits = run(s.vault.search_simple("tell me about Jennifer Key"))
        self.assertEqual(hits[0]["filename"], "People/Family/Jennifer Key.md")
        # one-word names must be capitalised, and names that are also words don't count at the start of a sentence
        self.assertEqual(s.assistant.match_people("Jen is coming over", [("Jen", "p.md")]), [("p.md", "person name")])
        self.assertEqual(s.assistant.match_people("who is jen", [("Jen", "p.md")]), [("p.md", "person name")])
        self.assertEqual(s.assistant.match_people("Will it rain?", [("Will", "w.md")]), [])
        self.assertEqual(s.assistant.match_people("Is Will coming?", [("Will", "w.md")]), [("w.md", "person name")])
        self.assertEqual(s.assistant.match_people("Is Jen coming?", [("Jen", "p.md")]), [("p.md", "person name")])
        self.assertEqual(s.assistant.match_people("will it rain", [("Will", "w.md")]), [])

    def test_birthdays_from_every_place_they_are_kept(self):
        s = self.services
        today = date.today()
        soon = today + timedelta(days=3)
        people = self.vault_dir / "People"
        (people / "Jennifer Key.md").write_text(
            f"---\nrelation: sister\nbirthday: {soon.replace(year=1986):%Y-%m-%d}\n---\n# Jennifer Key\n")
        (people / "Tom Hill.md").write_text("# Tom Hill\n\n- **Born:** 12th March 1990\n")
        (people / "Ann Lee.md").write_text("# Ann Lee\nFriend from work.\n")
        (self.vault_dir / "Inbox").mkdir()
        (self.vault_dir / "Inbox" / "Captures.md").write_text("- Ann Lee's birthday is 2 July\n")
        s.db.execute("INSERT INTO contacts (resource_name, etag, name, path, data, birthday) VALUES "
                     "('people/1', 'e', 'Bob Stone', 'People/Bob Stone.md', '{}', '--11-05')")
        run(s.vault.refresh())
        found = {b.name: b for b in run(s.assistant.birthdays())}
        self.assertEqual((found["Jennifer Key"].month, found["Jennifer Key"].year), (soon.month, 1986))
        self.assertEqual(found["Jennifer Key"].relation, "sister")
        self.assertEqual((found["Tom Hill"].day, found["Tom Hill"].month, found["Tom Hill"].year), (12, 3, 1990))
        self.assertEqual((found["Ann Lee"].day, found["Ann Lee"].month), (2, 7))
        self.assertEqual(found["Ann Lee"].path, "People/Ann Lee.md")
        self.assertEqual((found["Bob Stone"].day, found["Bob Stone"].month, found["Bob Stone"].year), (5, 11, None))

        async def ask(text):
            return [e async for e in s.assistant.handle(text)]

        def said(events):
            return "".join(e.get("text", "") for e in events if e["type"] == "token")
        events = run(ask("When is Jennifer's birthday?"))
        self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "people")
        self.assertIn("Jennifer Key (sister)", said(events))
        self.assertIn(f"turns {soon.year - 1986}", said(events))
        events = run(ask("Any birthdays coming up?"))
        self.assertIn("Jennifer Key", said(events))  # soonest first
        # brief uses the same collector
        self.assertTrue(any("Jennifer Key" in line for line in run(s.brief.birthdays(today))))
        # model offline → the script's list is the answer
        s.llm.base_url = "http://127.0.0.1:9"
        self.assertIn("All known birthdays", said(run(ask("any birthdays this month?"))))

    def test_looks_everywhere_when_the_first_place_has_no_answer(self):
        s = self.services
        (self.vault_dir / "Notes" / "Zoo.md").write_text("# Zoo\nWe saw a zebra at the zoo.\n")
        run(s.vault.refresh())
        events = [e for e in run(self._collect(s.assistant.handle("what do my notes say about the zebra?")))]
        routes = [e["route"] for e in events if e["type"] == "meta"]
        self.assertEqual(routes[0], "vault")
        self.assertEqual(routes[-1], "everywhere")
        self.assertTrue(any(e["type"] == "status" for e in events))
        self.assertFalse(any("don't mention" in e.get("text", "") for e in events))  # the unsure reply never shown
        final = self.ollama.requests[-1]["messages"][-1]["content"]
        self.assertIn("SOURCE DATA (looked in", final)
        self.assertIn("zebra", final)
        self.assertNotIn("online", final.split("\n", 3)[2])  # "my" question: never sent to the web

    @staticmethod
    async def _collect(generator):
        return [e async for e in generator]

    def test_wrong_user_gives_a_clear_error(self):
        self.services.vault.writer.base = self.services.vault.writer.base.replace("/chris", "/Chris")
        self.services.vault.writer.auth = ("chris", "app-pass")
        with self.assertRaises(VaultError) as caught:
            run(self.services.vault.put_text("New/Folder/X.md", "x"))
        self.assertIn("NEXTCLOUD_USER", str(caught.exception))

    def test_bad_app_password(self):
        self.services.vault.writer.auth = ("chris", "wrong")
        health = run(self.services.vault.health())
        self.assertFalse(health["ok"])
        with self.assertRaises(VaultError):
            run(self.services.vault.put_text("X.md", "x"))
