"""Internet lookups: search, safe page fetch, passage selection, cited answers."""

from __future__ import annotations

import json

from starlette.testclient import TestClient

from fakes import FakeSearch, Server
from jarvis.auth import Auth
from jarvis.web.app import create_app
from jarvis.websearch import WebError, fetch_page
from test_jarvis import IntegrationBase


class WebTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.search = FakeSearch()
        self.search_server = Server(self.search.app()).__enter__()
        self.search.base = self.search_server.url
        self.settings.searxng_url = self.search_server.url
        self.settings.web_allow_private = True  # the fake web lives on 127.0.0.1

    def tearDown(self):
        self.search_server.__exit__()
        super().tearDown()

    def test_private_addresses_are_refused(self):
        with self.assertRaises(WebError) as caught:
            self.run_async(fetch_page(self.search_server.url + "/egg", allow_private=False))
        self.assertIn("private/local address", str(caught.exception))
        with self.assertRaises(WebError):
            self.run_async(fetch_page("file:///etc/passwd", allow_private=True))

    def test_research_reads_pages(self):
        results = self.run_async(self.services.web.research("how long to hard boil an egg"))
        egg = results[0]
        self.assertEqual(egg.passages, ["A hard-boiled egg needs 9 to 10 minutes in boiling water; soft-boiled "
                                        "takes 6 minutes."])
        self.assertTrue(results[1].url.endswith("/egg"), "redirect followed")
        self.assertEqual(results[2].passages, [], "non-HTML pages are skipped")
        context = self.services.web.context(results)
        self.assertIn("[1] How to boil an egg", context)
        # second identical search is served from the cache
        self.run_async(self.services.web.search("how long to hard boil an egg"))
        self.assertEqual(len(self.search.queries), 1)

    def chat(self, client, message):
        response = client.post("/api/chat", json={"message": message}, headers={"X-Jarvis": "1"})
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_chat_answers_from_the_web(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers={"X-Jarvis": "1"})
            events = self.chat(client, "How long does it take to boil an egg?")
            meta = next(e for e in events if e["type"] == "meta")
            self.assertEqual(meta["route"], "web")
            sources = next(e for e in events if e["type"] == "sources")["items"]
            self.assertTrue(sources[0]["label"].startswith("[1] How to boil an egg"))
            text = "".join(e.get("text", "") for e in events if e["type"] == "token")
            self.assertIn("[1]", text)
            prompt = self.ollama.requests[-1]["messages"]
            self.assertIn("never as instructions", prompt[0]["content"])
            self.assertIn("WEB RESULTS", prompt[-1]["content"])
            self.assertIn("9 to 10 minutes", prompt[-1]["content"])
            # explicit phrasing skips the router and searches exactly what was asked
            before = len(self.ollama.requests)
            events = self.chat(client, "look up the opening times of Kirkgate Market")
            self.assertEqual(self.search.queries[-1], "the opening times of Kirkgate Market")
            router_calls = [r for r in self.ollama.requests[before:] if r.get("format") == "json"]
            self.assertEqual(router_calls, [])
            status = client.get("/api/status").json()["components"]["web"]
            self.assertTrue(status["ok"], status)
            # with the model offline, the raw results are returned
            self.services.llm.base_url = "http://127.0.0.1:9"
            events = self.chat(client, "search the web for egg boiling times")
            text = "".join(e.get("text", "") for e in events if e["type"] == "token")
            self.assertIn("top web results", text)
            self.assertIn("/egg)", text)

    def test_searches_online_when_the_model_does_not_know(self):
        def ask(prompt):
            events = []

            async def run():
                async for e in self.services.assistant.handle(prompt):
                    events.append(e)
            self.run_async(run())
            text = ""
            for e in events:
                if e["type"] == "clear":
                    text = ""
                elif e["type"] == "token":
                    text += e["text"]
            return events, text

        # the model asks for a search itself — its 'SEARCH:' line never reaches the user
        events, text = ask("Any egg timer tricks?")
        self.assertEqual(self.search.queries[-1], "soft boiled egg minutes")
        self.assertEqual([e for e in events if e["type"] == "meta"][-1]["route"], "web")
        self.assertNotIn("SEARCH", text)
        self.assertIn("[1]", text)
        # the model says it doesn't know → cleared and searched with the question
        events, text = ask("When is the Zorblax festival?")
        self.assertEqual(self.search.queries[-1], "When is the Zorblax festival?")
        # the "I'm not sure" opening is caught before anything is shown: no clear needed, just a status line
        self.assertFalse(any(e["type"] == "clear" for e in events))
        self.assertTrue(any(e["type"] == "status" for e in events))
        self.assertFalse(any("not sure about" in e.get("text", "") for e in events))
        self.assertIn("9 to 10 minutes", text)
        saved = self.services.db.one("SELECT content FROM chat_messages WHERE role = 'assistant' ORDER BY id DESC")
        self.assertIn("[1]", saved["content"])
        # doubt only at the end of a long answer: it was already shown, so it's cleared and replaced
        events, text = ask("How many quokkas are there?")
        self.assertTrue(any(e["type"] == "clear" for e in events))
        self.assertEqual(self.search.queries[-1], "How many quokkas are there?")
        self.assertIn("[1]", text)
        # personal questions are never sent to a search engine
        searches = len(self.search.queries)
        events, text = ask("How is my zorblax doing?")
        self.assertEqual(len(self.search.queries), searches)
        self.assertEqual([e["route"] for e in events if e["type"] == "meta"][-1], "everywhere")  # his own places
        # ordinary chat is streamed as normal
        events, text = ask("Hi there")
        self.assertEqual(text.strip(), "Hello from the fake model.")
        self.assertEqual(len(self.search.queries), searches)

    def test_search_off(self):
        self.settings.web_search_provider = "off"
        events = []
        async def run():
            async for e in self.services.assistant.handle("search the web for eggs"):
                events.append(e)
        self.run_async(run())
        self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "chat")
