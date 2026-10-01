"""Run with:  PYTHONPATH=src python -m unittest discover -s tests -v"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from fakes import API_KEY, FakeObsidian, FakeOllama, Server, free_port
from jarvis.auth import Auth, hash_password, totp_code, verify_password
from jarvis.config import Settings
from jarvis.db import Database
from jarvis.google.gmail import parse_message
from jarvis.notify import in_quiet_hours
from jarvis.pipelines.calendar import CalendarPipeline
from jarvis.pipelines.gmail import GmailPipeline
from jarvis.services import Services
from jarvis.vault.markdown import merge_frontmatter, read_block, replace_block, split_frontmatter
from jarvis.web.app import create_app


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(mid: str, tid: str, sender: str, subject: str, body: str, labels=None, extra_headers=None,
                  ts: float | None = None) -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": "Chris <chris@example.com>"},
               {"name": "Subject", "value": subject}]
    headers += extra_headers or []
    return {
        "id": mid, "threadId": tid, "labelIds": labels or ["INBOX", "CATEGORY_PERSONAL", "IMPORTANT"],
        "internalDate": str(int((ts or time.time()) * 1000)), "snippet": body[:50],
        "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(body)}},
            {"mimeType": "text/html", "body": {"data": b64("<p>ignored</p>")}},
        ]},
    }


class MarkdownTests(unittest.TestCase):
    def test_block_replacement_preserves_user_text(self):
        body = "# Sam\n\nMy own words.\n"
        once = replace_block(body, "timeline", "- one", heading="## Timeline")
        self.assertIn("My own words.", once)
        twice = replace_block(once + "\nMore of my words.\n", "timeline", "- two")
        self.assertIn("- two", twice)
        self.assertNotIn("- one", twice)
        self.assertIn("More of my words.", twice)
        self.assertEqual(twice.count("## Timeline"), 1)
        self.assertEqual(read_block(twice, "timeline"), "- two")

    def test_frontmatter_merge_never_overwrites_user_values(self):
        fm, body = split_frontmatter("---\nrelation: cousin\nemails: [a@x.com]\n---\n# Sam\n")
        merged = merge_frontmatter(fm, {"relation": "friend", "emails": ["b@x.com"], "type": "person"})
        self.assertEqual(merged["relation"], "cousin")
        self.assertEqual(merged["emails"], ["a@x.com", "b@x.com"])
        self.assertEqual(merged["type"], "person")
        self.assertEqual(body, "# Sam\n")


class GmailParseTests(unittest.TestCase):
    def test_personal_message_body_is_cleaned(self):
        body = "Hi Chris,\nMoving to Leeds in November!\n\nOn Mon, 1 Sep 2026 Chris wrote:\n> old text"
        parsed = parse_message(gmail_message("m1", "t1", "Sam Jones <Sam@Example.com>", "House move", body))
        self.assertEqual(parsed.from_addr, "sam@example.com")
        self.assertEqual(parsed.from_name, "Sam Jones")
        self.assertIn("Leeds", parsed.body)
        self.assertNotIn("old text", parsed.body)
        self.assertFalse(parsed.bulk)
        self.assertTrue(parsed.personal)

    def test_bulk_detection(self):
        promo = parse_message(gmail_message("m2", "t2", "Shop <deals@shop.com>", "Sale", "50% off",
                                            labels=["INBOX", "CATEGORY_PROMOTIONS"]))
        self.assertTrue(promo.bulk)
        listy = parse_message(gmail_message("m3", "t3", "News <news@site.com>", "Weekly", "hi",
                                            extra_headers=[{"name": "List-Unsubscribe", "value": "<mailto:x>"}]))
        self.assertTrue(listy.bulk)
        noreply = parse_message(gmail_message("m4", "t4", "no-reply@bank.com", "Statement", "hi"))
        self.assertTrue(noreply.bulk)


class AuthTests(unittest.TestCase):
    def test_password_and_totp(self):
        stored = hash_password("correct horse battery")
        self.assertTrue(verify_password("correct horse battery", stored))
        self.assertFalse(verify_password("wrong", stored))
        db = Database(":memory:")
        auth = Auth(db)
        auth.set_password("correct horse battery")
        secret = auth.begin_totp()
        self.assertTrue(auth.confirm_totp(totp_code(secret, int(time.time() // 30))))
        self.assertIsNone(auth.login("1.1.1.1", "correct horse battery", "000000"))
        code = totp_code(secret, int(time.time() // 30) + 1)
        token = auth.login("1.1.1.1", "correct horse battery", code)
        self.assertTrue(token and auth.session_valid(token))
        self.assertIsNone(auth.login("1.1.1.1", "correct horse battery", code), "codes cannot be reused")

    def test_quiet_hours(self):
        from datetime import datetime
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 23, 0), "22:00-07:00"))
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 6, 59), "22:00-07:00"))
        self.assertFalse(in_quiet_hours(datetime(2026, 1, 1, 12, 0), "22:00-07:00"))


class FakeGmail:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = {m["id"]: m for m in messages}

    async def profile(self):
        return {"emailAddress": "chris@example.com", "historyId": "100"}

    async def list_message_ids(self, query, limit=500):
        return list(self.messages)

    async def history(self, start):
        return [], start

    async def message(self, mid):
        return self.messages[mid]


class FakeCalendar:
    def __init__(self, events):
        self._events = events

    async def events(self, calendar_id, time_min, time_max, query=None, limit=1000):
        return self._events


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.obsidian = FakeObsidian()
        self.obsidian.files["People/Sam Jones.md"] = (
            "---\nemails: [sam@example.com]\nrelation: friend\ntags: [person]\n---\n# Sam Jones\n\nMet at uni.\n")
        self.obsidian.files["Notes/Hiking.md"] = "# Hiking\nPlanning a trip with [[Sam Jones]] #outdoors\n"
        self.ollama = FakeOllama()
        self.obs_server = Server(self.obsidian.app()).__enter__()
        self.llm_server = Server(self.ollama.app()).__enter__()
        self.settings = Settings(public_url="http://localhost:8080", data_dir=Path(self.tmp.name),
                                 secure_cookies=False, ollama_url=self.llm_server.url, chat_model="test-model",
                                 obsidian_url=self.obs_server.url, obsidian_api_key=API_KEY,
                                 obsidian_verify_tls=False, vault_backend="obsidian", notify_vip_senders=["sam@example.com"])
        self.services = Services(self.settings)

    def tearDown(self):
        self.obs_server.__exit__()
        self.llm_server.__exit__()
        self.services.db.close()
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)


class IntegrationTests(IntegrationBase):

    def test_gmail_pipeline_writes_notes_and_preserves_user_text(self):
        s = self.services
        self.run_async(s.writer.refresh_people())
        s.gmail_pipeline.gmail = FakeGmail([
            gmail_message("m1", "t1", "Sam Jones <sam@example.com>", "House move", "Moving to Leeds in November"),
            gmail_message("m2", "t2", "Shop <deals@shop.com>", "Sale", "50% off", labels=["CATEGORY_PROMOTIONS"]),
            gmail_message("m3", "t3", "Alex Brown <alex@example.org>", "Lunch?", "Lunch on Friday?"),
        ])
        result = self.run_async(s.gmail_pipeline.run())
        self.assertEqual(result["new"], 3)
        self.assertEqual(result["bulk_skipped"], 1)
        flushed = self.run_async(s.writer.flush())
        self.assertFalse(flushed["offline"])
        files = self.obsidian.files
        thread_paths = [p for p in files if p.startswith("Sources/Email/")]
        self.assertEqual(len(thread_paths), 2, thread_paths)
        self.assertIn("People/Alex Brown.md", files, "new correspondent gets a person note")
        sam = files["People/Sam Jones.md"]
        self.assertIn("Met at uni.", sam)
        self.assertIn("relation: friend", sam)
        self.assertIn("House move", sam)
        journal = [p for p in files if p.startswith("Journal/")]
        self.assertTrue(journal)
        self.assertIn("Email from [[People/Sam Jones", files[journal[0]])
        # user edits the person note; a re-render keeps them
        files["People/Sam Jones.md"] = sam.replace("Met at uni.", "Met at uni. Loves climbing.")
        s.db.queue_note("person", "sam@example.com")
        self.run_async(s.writer.flush())
        self.assertIn("Loves climbing.", files["People/Sam Jones.md"])
        # revert the creation of Alex's note
        change = s.db.one("SELECT id FROM note_history WHERE path = 'People/Alex Brown.md'")
        self.run_async(s.writer.revert(change["id"]))
        self.assertIn("reverted", files["People/Alex Brown.md"])

    def test_calendar_pipeline(self):
        s = self.services
        start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 1800))
        end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 5400))
        s.calendar_pipeline.calendar = FakeCalendar([{
            "event_id": "ev123456", "calendar_id": "primary", "summary": "Climbing with Sam", "start": start,
            "end": end, "all_day": False, "location": "The Wall", "description": "", "status": "confirmed",
            "attendees": [{"email": "sam@example.com", "name": "Sam Jones", "self": False}],
            "updated": "1", "html_link": "https://calendar.google.com/x"}])
        self.run_async(s.writer.refresh_people())
        self.assertEqual(self.run_async(s.calendar_pipeline.run())["changed"], 1)
        self.assertEqual(self.run_async(s.calendar_pipeline.run())["changed"], 0, "unchanged events are skipped")
        self.run_async(s.writer.flush())
        event_notes = [p for p in self.obsidian.files if p.startswith("Sources/Calendar/")]
        self.assertEqual(len(event_notes), 1)
        self.assertIn("[[People/Sam Jones]]", self.obsidian.files[event_notes[0]])
        self.assertEqual(self.run_async(s.calendar_pipeline.remind()), 1)
        self.assertEqual(self.run_async(s.calendar_pipeline.remind()), 0)

    def test_outbox_waits_while_obsidian_offline(self):
        s = self.services
        s.vault.base_url = f"http://127.0.0.1:{free_port()}"  # nothing listening
        s.db.execute("INSERT INTO captures (ts, day, text) VALUES (?, '2026-09-28', 'test')", (time.time(),))
        s.db.queue_note("inbox", "2026-09-28")
        result = self.run_async(s.writer.flush())
        self.assertTrue(result["offline"])
        self.assertEqual(result["pending"], 1)
        s.vault.base_url = self.obs_server.url
        self.assertEqual(self.run_async(s.writer.flush())["written"], 1)
        self.assertIn("Inbox/2026-09-28 Captures.md", self.obsidian.files)

    def test_notification_categories(self):
        n = self.services.notifier
        self.settings.notify_quiet_hours = "00:00-23:59"  # always quiet for this test
        prefs = n.preferences()
        self.assertEqual((prefs["saves"]["on"], prefs["brief"]["quiet"]), (False, False))
        self.assertEqual(self.run_async(n.notify("Saved 3 notes", "…", 2, category="saves")), "off")
        self.assertIsNone(self.services.db.one("SELECT 1 FROM notifications WHERE title = 'Saved 3 notes'"))
        # the brief ignores quiet hours (sent — or 'logged' here, as ntfy isn't configured); email is held
        self.assertEqual(self.run_async(n.notify("Good morning", "…", 3, category="brief")), "logged")
        self.assertEqual(self.run_async(n.notify("Email from Sam", "…", 3, category="email")), "held")
        n.set_preferences({"email": {"on": True, "quiet": False}, "brief": {"on": False}})
        self.assertEqual(self.run_async(n.notify("Email from Bob", "…", 3, category="email")), "logged")
        self.assertEqual(self.run_async(n.notify("Good morning 2", "…", 3, category="brief")), "off")
        self.assertEqual(n.preferences()["email"], {"label": "Important email", "on": True, "quiet": False})

    def test_model_is_kept_loaded(self):
        llm = self.services.llm
        self.assertEqual(llm.keep_alive, -1)
        self.assertIn("not loaded yet", self.run_async(llm.health())["detail"])
        self.assertEqual(self.run_async(llm.warm()), "loaded test-model")
        load = next(r for r in self.ollama.requests if "messages" not in r)
        self.assertEqual(load, {"model": "test-model", "keep_alive": -1})
        self.assertEqual(self.run_async(llm.warm()), "model in memory")
        self.assertIn("in GPU memory", self.run_async(llm.health())["detail"])
        self.run_async(llm.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(self.ollama.requests[-1]["keep_alive"], -1)  # every request keeps it loaded
        from jarvis.llm import Ollama
        self.assertEqual(Ollama("http://x", "m", "m", keep_alive="2h").keep_alive, "2h")
        offline = Ollama("http://127.0.0.1:9", "m", "m")
        self.assertEqual(self.run_async(offline.warm()), "Ollama offline")

    def test_web_login_chat_and_remember(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        self.run_async(self.services.writer.refresh_people())
        with TestClient(app, base_url="http://localhost:8080") as client:
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(client.post("/api/login", json={"password": "x"}).status_code, 403, "needs header")
            h = {"X-Jarvis": "1"}
            self.assertEqual(client.post("/api/login", json={"password": "wrong"}, headers=h).status_code, 401)
            self.assertEqual(client.post("/api/login", json={"password": "a very long password"}, headers=h)
                             .status_code, 200)
            self.assertIn("default-src 'self'", client.get("/").headers["content-security-policy"])
            status = client.get("/api/status").json()
            self.assertTrue(status["components"]["model"]["ok"], status)
            self.assertTrue(status["components"]["obsidian"]["ok"], status)

            def chat(message):
                response = client.post("/api/chat", json={"message": message}, headers=h)
                events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
                return events, "".join(e.get("text", "") for e in events if e["type"] == "token")

            events, text = chat("Remember that Sam Jones is allergic to peanuts")
            self.assertIn("Saved “Sam Jones is allergic to peanuts”", text)
            self.assertIn("What I saved", text)
            self.assertIn("Inbox/", text)
            inbox = [p for p in self.obsidian.files if p.startswith("Inbox/")]
            self.assertTrue(inbox)
            self.assertIn("[[People/Sam Jones|Sam Jones]] is allergic to peanuts", self.obsidian.files[inbox[0]])

            events, text = chat("What do my notes say about Sam?")
            self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "vault")
            sources = next(e for e in events if e["type"] == "sources")["items"]
            self.assertTrue(any(s["label"] == "Sam Jones" for s in sources), sources)
            self.assertIn("Sam Jones is mentioned", text)
            prompt = self.ollama.requests[-1]["messages"][-1]["content"]
            self.assertIn("Linked from:", prompt)

            events, text = chat("Show notes tagged #outdoors")
            sources = next(e for e in events if e["type"] == "sources")["items"]
            self.assertTrue(any(s["label"] == "Hiking" for s in sources), sources)

            events, text = chat("hello there")
            self.assertEqual(text.strip(), "Hello from the fake model.")

            events, text = chat("Send an email to Sam")
            self.assertIn("can't make that kind of change", text)

            changes = client.get("/api/vault/changes").json()
            self.assertTrue(changes["changes"])


if __name__ == "__main__":
    unittest.main()
