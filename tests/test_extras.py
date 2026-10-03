"""Shared screenshots (OCR), birthday heads-up with gift ideas, adding to notes, Home Assistant triggers."""

from __future__ import annotations

import io
import time
import unittest
from datetime import date, datetime, timedelta

from starlette.testclient import TestClient

from jarvis.auth import Auth
from jarvis.ocr import available as ocr_available
from jarvis.pipelines.birthdays import gift_ideas
from jarvis.web.app import create_app
from test_jarvis import IntegrationBase

H = {"X-Jarvis": "1"}


class ExtrasTests(IntegrationBase):
    def client(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        client = TestClient(app, base_url="http://localhost:8080")
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.post("/api/login", json={"password": "a very long password"}, headers=H)
        return client

    # ---------------------------------------------------------------- 1. shared screenshots
    @unittest.skipUnless(ocr_available(), "tesseract not installed")
    def test_shared_screenshot_is_read(self):
        from PIL import Image, ImageDraw, ImageFont
        client = self.client()
        image = Image.new("RGB", (720, 900), (11, 20, 26))  # WhatsApp in dark mode: a green bubble on near-black
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=34)
        draw.rounded_rectangle((40, 300, 680, 470), 20, fill=(0, 92, 75))
        draw.text((70, 330), "Bowling Saturday 6pm", fill=(235, 235, 235), font=font)
        draw.text((70, 390), "Hollywood Bowl, Broadway Plaza", fill=(235, 235, 235), font=font)
        png = io.BytesIO()
        image.save(png, "PNG")
        response = client.post("/api/ocr", content=png.getvalue(), headers=H | {"Content-Type": "image/png"})
        self.assertEqual(response.status_code, 200, response.text)
        text = response.json()["text"]
        self.assertIn("Bowling Saturday", text)
        self.assertIn("Hollywood Bowl", text)
        self.assertEqual(client.post("/api/ocr", content=b"", headers=H).status_code, 400)
        self.assertEqual(client.post("/api/ocr", content=b"not an image", headers=H).status_code, 400)

    def test_share_target_fallback_without_the_service_worker(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        with TestClient(app, base_url="http://localhost:8080") as client:  # not signed in: it only redirects
            boundary = "XyZ"
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nSee you at 6 & bring £5\r\n"
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a.png\"\r\n"
                    f"Content-Type: image/png\r\n\r\n\x89PNG\r\n--{boundary}--\r\n").encode()
            response = client.post("/share-target", content=body, follow_redirects=False,
                                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"],
                             "/?share_text=See+you+at+6+%26+bring+%C2%A35&share_note=image")
            manifest = client.get("/manifest.webmanifest").json()
            self.assertEqual(manifest["share_target"]["params"]["files"][0]["accept"], ["image/*"])
            self.assertIn("blob:", client.get("/").headers["content-security-policy"])

    # ---------------------------------------------------------------- 2. birthdays
    def test_gift_ideas_from_a_note(self):
        note = ("---\nbirthday: 1990-10-10\n---\n# Sam Jones\nMet at uni. Loves climbing and anything by Terry Pratchett.\n"
                "Allergic to peanuts; doesn't like chocolate.\n\n## Gift ideas\n- Climbing chalk bag\n- [[Books/Mort|Mort]] "
                "(hardback)\n\n## Timeline\n- 2026-01-02 email\n")
        self.assertEqual(gift_ideas(note), ["Loves climbing and anything by Terry Pratchett.", "Climbing chalk bag",
                                            "Mort (hardback)"])

    def test_birthday_heads_up_a_week_before(self):
        s = self.services
        tz = self.settings.tz
        now = datetime.now(tz)
        week = now.date() + timedelta(days=7)
        from jarvis.assistant.facts import Birthday
        self.obsidian.files["People/Sam Jones.md"] = "# Sam Jones\n## Gift ideas\n- Chalk bag\n"
        people = [Birthday("Sam Jones", "People/Sam Jones.md", week.month, week.day, 1986, ["note property"]),
                  Birthday("Alex", "", 1, 1, None, ["Google Contacts"])]

        async def source():  # where they come from is tested elsewhere (contacts, notes, mentions)
            return people
        s.birthday_reminders.source = source
        if now.hour < 9:
            self.skipTest("heads-ups go out after 09:00")
        self.assertEqual(self.run_async(s.birthday_reminders.run())["sent"], 1)
        row = s.db.one("SELECT title, message, url FROM notifications WHERE title LIKE '🎂%'")
        self.assertEqual(row["title"], f"🎂 Sam Jones turns {week.year - 1986} on {week:%a} {week.day} {week:%b}")
        self.assertIn("- Chalk bag", row["message"])
        self.assertTrue(row["url"].endswith("/#note?path=People%2FSam%20Jones.md"))
        self.assertEqual(self.run_async(s.birthday_reminders.run())["sent"], 0, "once")

    # ---------------------------------------------------------------- 3. adding to a note from the phone
    def test_adding_to_a_note(self):
        client = self.client()
        self.obsidian.files["People/Sam Jones.md"] = "# Sam Jones\nMet at uni.\n"
        response = client.post("/api/vault/note/append", json={"path": "People/Sam Jones.md",
                                                                "text": "Likes Earl Grey\nand lemon drizzle"}, headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        text = self.obsidian.files["People/Sam Jones.md"]
        self.assertRegex(text, r"Met at uni\.\n\n- \d{4}-\d\d-\d\d \d\d:\d\d — Likes Earl Grey\n  and lemon drizzle\n$")
        self.assertEqual(client.post(f"/api/vault/revert/{response.json()['change_id']}", headers=H).status_code, 200)
        self.assertEqual(self.obsidian.files["People/Sam Jones.md"], "# Sam Jones\nMet at uni.\n")
        for bad in ({"path": "People/Nobody.md", "text": "x"}, {"path": ".obsidian/app.md", "text": "x"},
                    {"path": "People/Sam Jones.md", "text": "  "}, {"path": "../etc/passwd", "text": "x"}):
            self.assertEqual(client.post("/api/vault/note/append", json=bad, headers=H).status_code, 400, bad)

    # ---------------------------------------------------------------- 4. Home Assistant triggers
    def test_home_assistant_triggers(self):
        client = self.client()
        s = self.services
        settings = client.get("/api/hooks").json()
        token = settings["token"]
        self.assertGreater(len(token), 20)
        self.assertEqual(client.post("/api/hook/morning").status_code, 401)
        self.assertEqual(client.post("/api/hook/morning", headers={"Authorization": "Bearer nope"}).status_code, 401)
        auth = {"Authorization": f"Bearer {token}"}  # no session cookie or X-Jarvis header needed
        from starlette.testclient import TestClient as Bare
        ha = Bare(client.app, base_url="http://localhost:8080")
        morning = ha.post("/api/hook/morning", headers=auth).json()
        self.assertIn(morning["brief"], ("sent", "already sent today"))
        self.assertTrue(morning["speech"].startswith("Good morning."))
        self.assertNotIn("**", morning["speech"])
        self.assertEqual(ha.post("/api/hook/morning", headers=auth).json()["speech"], "", "debounced")
        s.hooks._last.clear()
        self.assertEqual(ha.post("/api/hook/morning", headers=auth).json()["brief"], "already sent today")
        self.assertEqual(ha.post("/api/hook/elsewhere", headers=auth).status_code, 404)
        home = ha.post(f"/api/hook/home?token={token}").json()
        self.assertTrue(home["text"].startswith("**Welcome home**"))
        self.assertTrue(home["speech"].startswith("Welcome home."))
        self.assertTrue(s.db.one("SELECT 1 FROM chat_messages WHERE role = 'activity' AND content LIKE '**Welcome home**%'"))
        evening = ha.post("/api/hook/evening", headers=auth).json()
        self.assertTrue(evening["speech"].startswith("Tomorrow:"))
        # a new token replaces the old one
        new = client.post("/api/hooks", headers=H).json()["token"]
        self.assertNotEqual(new, token)
        s.hooks._last.clear()
        self.assertEqual(ha.post("/api/hook/home", headers=auth).status_code, 401)
