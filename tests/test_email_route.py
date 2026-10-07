"""'Look in my email for …' searches email (not a chat answer), loosening the search when it finds nothing."""

from __future__ import annotations

from starlette.testclient import TestClient

from jarvis.assistant.planner import explicit_email, gmail_fallbacks
from jarvis.auth import Auth
from jarvis.web.app import create_app
from test_jarvis import IntegrationBase


class EmailRouteTests(IntegrationBase):
    def test_parsing(self):
        cases = {
            "Look in my email for life group notices": "life group notices",
            "search email for the life group rota": "life group rota",
            "any emails about life group?": "life group",
            "Any emails from Sam Jones?": 'from:"Sam Jones"',
            "have a look through my inbox for anything from the council": "from:council",
            "what did Sam say": None, "Search my email": "", "can you check my emails for that?": "",
            "no, look in my inbox instead": "", "email Sam about dinner": None, "find the boiler email": None,
        }
        for prompt, expected in cases.items():
            self.assertEqual(explicit_email(prompt), expected, prompt)
        self.assertEqual(gmail_fallbacks("life group notices"), ["life group"])
        self.assertEqual(gmail_fallbacks("from:council"), [])

    def test_looks_in_email_and_loosens(self):
        s = self.services
        searched = []

        async def search_threads(query, limit=15):
            searched.append(query)
            if query != "life group":
                return []
            return [{"id": "t1", "subject": "Life group this week", "from": "Ben <ben@example.com>", "date": "",
                     "messages": 1, "snippet": "We're meeting at the Toplisses' on Thursday"}]
        s.assistant.gmail.search_threads = search_threads

        async def ask(text):
            return [e async for e in s.assistant.handle(text)]
        events = self.run_async(ask("Look in my email for life group notices"))
        meta = next(e for e in events if e["type"] == "meta")
        self.assertEqual((meta["route"], meta["query"]), ("gmail", "life group notices"))
        self.assertEqual(searched, ["life group notices", "life group"])
        sources = next(e for e in events if e["type"] == "sources")["items"]
        self.assertEqual(sources[0]["label"], "Ben <ben@example.com> — Life group this week")

    def test_search_my_email_follows_the_question_before(self):
        s = self.services
        searched = []

        async def search_threads(query, limit=15):
            searched.append(query)
            return [{"id": "t1", "subject": "Life group", "from": "Ben", "date": "", "messages": 1, "snippet": "Thu"}]
        s.assistant.gmail.search_threads = search_threads
        s.db.execute("INSERT INTO chat_messages (ts, role, content, trace) VALUES (1, 'user', 'When is life group this week?', 'a1'),"
                     " (1, 'assistant', 'I am not sure.', 'a1')")

        async def ask(text):
            return [e async for e in s.assistant.handle(text)]
        events = self.run_async(ask("Search my email"))
        meta = next(e for e in events if e["type"] == "meta")
        self.assertEqual((meta["route"], meta["query"]), ("gmail", "life group week"))
        self.assertEqual(searched, ["life group week"])

    def test_report_file_has_everything(self):
        s = self.services
        s.db.execute("INSERT INTO chat_messages (ts, role, content, trace) VALUES (1, 'user', 'When is life group?', 'q1'),"
                     " (1, 'assistant', 'No idea.', 'q1'), (2, 'user', 'Look in my email for life group notices', 'q2'),"
                     " (2, 'assistant', 'Hello.', 'q2')")
        s.db.execute("INSERT INTO logs (ts, level, source, trace, message, data, error) VALUES (1, 20, 'router', 'q1', "
                     "\"model chose 'chat'\", '{}', '')")
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(s.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            saved = client.post("/api/feedback", json={"trace": "q2", "note": "Should have searched email"}, headers=h).json()
            report = client.get(saved["download"]).json()["reports"][0]
            self.assertEqual((report["prompt"], report["answer"], report["note"]),
                             ("Look in my email for life group notices", "Hello.", "Should have searched email"))
            self.assertEqual([m["content"] for m in report["before"]], ["When is life group?", "No idea."])
            self.assertEqual(report["before"][0]["logs"][0]["message"], "model chose 'chat'")
            # exporting the 👎 line's own trace brings the report too
            trace = s.db.one("SELECT trace FROM logs WHERE source = 'feedback' ORDER BY id DESC LIMIT 1")["trace"]
            exported = client.get("/api/diag/export", params={"trace": trace}).json()
            self.assertEqual(exported["reports"][0]["id"], saved["id"])


class ReportTwoTests(IntegrationBase):
    """jarvis-report-2: three ways of asking for Lucy Kitchin's life group notices."""

    def setUp(self):
        super().setUp()
        s = self.services
        import json as _json
        import time as _time
        for n, (mid, subject, body) in enumerate((("m1", "Life Group Notices", "Prayer and fasting week from Monday 12th."),
                                                  ("m2", "Life Group Notices", "Old notices from September."))):
            s.db.execute("INSERT INTO emails (message_id, thread_id, ts, from_addr, from_name, to_addrs, subject, labels, "
                         "bulk, outgoing, snippet, body, attachments) VALUES (?, ?, ?, 'kings@mg.churchsuite.com', "
                         "'Lucy Kitchin', ?, ?, '[]', 1, 0, '', ?, '[]')",
                         (mid, f"t{mid}", _time.time() - 86400 * (1 + n * 20), _json.dumps([["me@example.com", "Chris"]]),
                          subject, body))
        self.searched = []

        async def search_threads(query, limit=15):
            self.searched.append(query)
            lowered = query.casefold()
            if "from:" in lowered and 'from:"lucy kitchin"' not in lowered:
                return []
            if "life group" not in lowered:
                return []
            return [{"id": "tm1", "subject": "Life Group Notices", "from": "Lucy Kitchin <kings@mg.churchsuite.com>",
                     "date": "", "messages": 1, "snippet": "Prayer and fasting"},
                    {"id": "tm2", "subject": "Life Group Notices", "from": "Lucy Kitchin <kings@mg.churchsuite.com>",
                     "date": "", "messages": 1, "snippet": "Old"}]
        s.assistant.gmail.search_threads = search_threads

    def ask(self, text):
        async def go():
            return [e async for e in self.services.assistant.handle(text)]
        return self.run_async(go())

    def test_question_about_an_email_subject_goes_to_email(self):
        events = self.ask("What are the life group notices?")
        meta = next(e for e in events if e["type"] == "meta")
        self.assertEqual((meta["route"], meta["query"]), ("gmail", "life group notices"))

    def test_misspelt_sender_and_topic(self):
        for prompt in ("Look for the most recent email from lucy kitchen about life group notices",
                       "Look for the most recent email from.lich kitchen about life group notices"):
            self.searched.clear()
            events = self.ask(prompt)
            meta = next(e for e in events if e["type"] == "meta")
            self.assertEqual(meta["route"], "gmail", prompt)
            self.assertEqual(self.searched[0], 'from:"Lucy Kitchin" life group notices', prompt)
            sources = next(e for e in events if e["type"] == "sources")["items"]
            self.assertTrue(sources and "Life Group Notices" in sources[0]["label"], prompt)

    def test_the_newest_email_is_read_in_full(self):
        context, _ = self.run_async(self.services.assistant.gather_gmail(
            'from:"lucy kitchen" life group notices', "the most recent email from lucy kitchen about life group notices"))
        self.assertIn("Prayer and fasting week from Monday 12th.", context)
        self.assertIn('"most_recent": true', context)

    def test_nothing_found_is_said_plainly(self):
        async def nothing(query, limit=15):
            self.searched.append(query)
            return []
        self.services.assistant.gmail.search_threads = nothing
        events = self.ask("Look for the most recent email from Zed Quux about the pottery club")
        text = "".join(e.get("text", "") for e in events if e["type"] == "token")
        self.assertTrue(text.startswith("I couldn't find an email matching that."), text)
        self.assertIn("“pottery club”", text)
