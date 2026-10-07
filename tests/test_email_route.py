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
