"""Diagnostics: traces, HTTP instrumentation, redaction, UI endpoints."""

from __future__ import annotations

import json
import time

from starlette.testclient import TestClient

from fakes import free_port
from jarvis import diag
from jarvis.auth import Auth
from jarvis.web.app import create_app
from test_jarvis import FakeGmail, IntegrationBase, gmail_message


class RedactionTests(IntegrationBase.__mro__[1]):
    def test_redact(self):
        data = {"refresh_token": "abc", "nested": {"client_secret": "s", "ok": "fine"}, "items": [{"password": "p"}]}
        self.assertEqual(diag.redact(data), {"refresh_token": "***", "nested": {"client_secret": "***", "ok": "fine"},
                                             "items": [{"password": "***"}]})
        self.assertEqual(diag.redact_url("https://x/y?key=123&q=hello"), "https://x/y?key=%2A%2A%2A&q=hello")
        self.assertTrue(diag.redact("x" * 10_000).endswith("chars)"))


class DiagTests(IntegrationBase):
    def logs(self, **where):
        rows = self.services.db.all("SELECT * FROM logs ORDER BY id")
        return [dict(r) for r in rows if all(r[k] == v for k, v in where.items())]

    def test_chat_trace_and_details(self):
        s = self.services
        app = create_app(self.settings, s, start_jobs=False)
        Auth(s.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            response = client.post("/api/chat", json={"message": "What do my notes say about Sam?"}, headers=h)
            events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
            trace_id = events[0]["id"]
            self.assertEqual(events[0]["type"], "trace")
            logs = client.get(f"/api/diag/logs?trace={trace_id}").json()
            messages = [e["message"] for e in logs["items"]]
            self.assertEqual(logs["trace"]["kind"], "chat")
            self.assertEqual(logs["trace"]["status"], "ok", [e for e in logs["items"] if e["level"] != "info"])
            self.assertTrue(any(m.startswith("model chose 'vault'") for m in messages), messages)
            self.assertTrue(any(m.startswith("vault candidates") for m in messages), messages)
            self.assertTrue(any("streamed answer" in m for m in messages), messages)
            history = client.get("/api/chat/history").json()
            self.assertEqual(history[-1]["trace"], trace_id)
            # verbose mode records prompts and HTTP bodies, with secrets redacted
            client.post("/api/diag/verbose", json={"minutes": 5}, headers=h)
            response = client.post("/api/chat", json={"message": "hello there"}, headers=h)
            trace_id = json.loads(response.text.splitlines()[0])["id"]
            items = client.get(f"/api/diag/logs?trace={trace_id}").json()["items"]
            http = [e for e in items if e["source"] == "ollama"]
            self.assertTrue(http and "request_body" in http[0]["data"], items)
            model = next(e for e in items if e["source"] == "model" and "streamed" in e["message"])
            self.assertIn("prompt", model["data"])
            client.post("/api/diag/verbose", json={"minutes": 0}, headers=h)
            # listing, meta, export and browser errors
            traces = client.get("/api/diag/traces?kind=chat").json()
            self.assertGreaterEqual(len(traces), 2)
            meta = client.get("/api/diag/meta").json()
            self.assertIn("model", meta["sources"])
            client.post("/api/diag/client", json={"message": "TypeError: x is undefined", "stack": "at app.js:1"},
                        headers=h)
            problems = client.get("/api/diag/traces?problems=1").json()
            self.assertTrue(any(t["kind"] == "client" for t in problems))
            export = client.get(f"/api/diag/export?trace={trace_id}")
            self.assertIn("attachment", export.headers["content-disposition"])
            self.assertTrue(json.loads(export.text)["logs"])
            entries = client.get("/api/diag/logs?level=warning").json()["items"]
            self.assertTrue(any(e["source"] == "browser" for e in entries))

    def test_job_traces_and_failures(self):
        s = self.services
        s.vault.base_url = f"http://127.0.0.1:{free_port()}"      # Obsidian offline
        s.llm.base_url = f"http://127.0.0.1:{free_port()}"        # model offline
        s.db.queue_note("inbox", "2026-09-29")
        s.db.execute("INSERT INTO captures (ts, day, text) VALUES (?, '2026-09-29', 'x')", (time.time(),))
        job = s.jobs["vault"]
        self.run_async(s.run_job(job, manual=True))
        trace = s.db.one("SELECT * FROM traces WHERE id = ?", (job.last_trace,))
        self.assertEqual(trace["kind"], "job")
        entries = [dict(r) for r in s.db.all("SELECT * FROM logs WHERE trace = ?", (job.last_trace,))]
        self.assertTrue(any(e["source"] == "obsidian" and "failed" in e["message"] for e in entries), entries)
        # quiet background ticks are not stored as traces
        before = s.db.one("SELECT COUNT(*) n FROM traces")["n"]
        self.run_async(s.run_job(s.jobs["digest"]))
        self.assertEqual(s.db.one("SELECT COUNT(*) n FROM traces")["n"], before)
        # a crashing job records the traceback
        async def boom():
            raise RuntimeError("kaboom")
        s.jobs["digest"].func = boom
        self.run_async(s.run_job(s.jobs["digest"]))
        error = s.db.one("SELECT * FROM logs WHERE level = 40 AND message LIKE '%kaboom%'")
        self.assertIn("Traceback", error["error"])
        self.assertEqual(s.db.one("SELECT status FROM traces WHERE id = ?", (error["trace"],))["status"], "warning")
        # the model being offline is explained in a chat trace
        with diag.trace("chat", "test") as t:
            self.run_async(s.assistant.plan("hello"))
        msgs = [r["message"] for r in s.db.all("SELECT message FROM logs WHERE trace = ?", (t["id"],))]
        self.assertTrue(any("keyword fallback" in m for m in msgs), msgs)

    def test_gmail_classification_logged(self):
        s = self.services
        s.diag.set_verbose(5)
        s.gmail_pipeline.gmail = FakeGmail([
            gmail_message("m2", "t2", "Shop <deals@shop.com>", "Sale", "50% off", labels=["CATEGORY_PROMOTIONS"])])
        with diag.trace("job", "gmail"):
            self.run_async(s.gmail_pipeline.run())
        row = s.db.one("SELECT data FROM logs WHERE source = 'gmail' AND message LIKE 'bulk:%'")
        self.assertIn("CATEGORY_PROMOTIONS", json.loads(row["data"])["reason"])
        self.assertEqual(s.diag.prune()["rows"], s.db.one("SELECT COUNT(*) n FROM logs")["n"])
