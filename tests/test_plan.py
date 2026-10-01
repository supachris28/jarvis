"""Contacts → People, reminders, Home Assistant scheduled actions, morning brief."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from fakes import FakeHomeAssistant, Server
from jarvis.auth import Auth
from jarvis.extract.when import next_occurrence, parse_when
from jarvis.google.contacts import normalize_contact
from jarvis.ha import parse_command
from jarvis.web.app import create_app
from test_jarvis import FakeGmail, IntegrationBase, gmail_message

TZ = ZoneInfo("Europe/London")


class ParsingTests(IntegrationBase.__mro__[1]):
    def test_when(self):
        now = datetime(2026, 9, 29, 10, 15, tzinfo=TZ)  # a Tuesday
        w = parse_when("remind me on 14 October 2027 at 18:30 to renew the passport", now)
        self.assertEqual((w.at, w.rest.split()), (datetime(2027, 10, 14, 18, 30, tzinfo=TZ),
                                                  ["remind", "me", "to", "renew", "the", "passport"]))
        w = parse_when("call the garage on Thursday at 9am", now)
        self.assertEqual((w.at, w.rest), (datetime(2026, 10, 1, 9, 0, tzinfo=TZ), "call the garage"))
        w = parse_when("every weekday at 7am turn on the coffee machine", now)
        self.assertEqual((w.at.hour, w.repeat, w.rest), (7, "weekdays", "turn on the coffee machine"))
        self.assertEqual(parse_when("in 20 minutes check the oven", now).at, now + timedelta(minutes=20))
        self.assertIsNone(parse_when("buy milk", now).at)
        friday = datetime(2026, 10, 2, 7, 0, tzinfo=TZ)
        self.assertEqual(next_occurrence(friday, "weekdays").weekday(), 0)

    def test_commands(self):
        self.assertEqual(parse_command("turn off the kitchen lights").verb, "off")
        self.assertEqual(parse_command("switch the coffee machine on").target, "the coffee machine")
        c = parse_command("set the living room thermostat to 21 degrees")
        self.assertEqual((c.verb, c.value), ("set", 21.0))
        self.assertIsNone(parse_command("what's the weather"))

    def test_contact_normalisation(self):
        c = normalize_contact({"resourceName": "people/c1", "etag": "e", "names": [{"displayName": "Sam Jones"}],
                               "emailAddresses": [{"value": "Sam@Example.com"}], "phoneNumbers": [{"value": "0770"}],
                               "birthdays": [{"date": {"month": 4, "day": 12}}],
                               "relations": [{"person": "Amy Jones", "formattedType": "Spouse"}]})
        self.assertEqual((c["emails"], c["birthday"], c["relations"][0]["type"]), (["sam@example.com"], "--04-12", "Spouse"))


class FakeContacts:
    def __init__(self, people):
        self.people = people

    async def all(self):
        return self.people


class PlanTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.home = FakeHomeAssistant()
        self.ha_server = Server(self.home.app()).__enter__()
        s = self.services
        s.ha.url, s.ha.token = self.ha_server.url, FakeHomeAssistant.TOKEN
        self.settings.ha_url = self.ha_server.url

    def tearDown(self):
        self.ha_server.__exit__()
        super().tearDown()

    def login(self, client):
        Auth(self.services.db).set_password("a very long password")
        client.post("/api/login", json={"password": "a very long password"}, headers={"X-Jarvis": "1"})

    def chat(self, client, message):
        response = client.post("/api/chat", json={"message": message}, headers={"X-Jarvis": "1"})
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_contacts_seed_people(self):
        s = self.services
        self.run_async(s.writer.refresh_people())
        today = datetime.now(TZ)
        s.contacts_pipeline.contacts = FakeContacts([
            normalize_contact({"resourceName": "people/c1", "etag": "1", "names": [{"displayName": "Samuel Jones"}],
                               "emailAddresses": [{"value": "sam@example.com"}], "phoneNumbers": [{"value": "07700 900123"}],
                               "birthdays": [{"date": {"year": 1990, "month": today.month, "day": today.day}}],
                               "relations": [{"person": "Amy Jones", "formattedType": "Spouse"}]}),
            normalize_contact({"resourceName": "people/c2", "etag": "1", "names": [{"displayName": "Amy Jones"}],
                               "phoneNumbers": [{"value": "07700 900456"}]}),
        ])
        self.assertEqual(self.run_async(s.contacts_pipeline.run()), {"contacts": 2, "changed": 2})
        self.run_async(s.writer.flush())
        files = self.obsidian.files
        sam = files["People/Sam Jones.md"]  # existing note found by email; no duplicate "Samuel Jones" note
        self.assertNotIn("People/Samuel Jones.md", files)
        self.assertIn("Met at uni.", sam)
        self.assertIn("07700 900123", sam)
        self.assertIn("Spouse: [[People/Amy Jones]]", sam)
        self.assertIn("## Contact", files["People/Amy Jones.md"])
        self.assertEqual(self.run_async(s.contacts_pipeline.run())["changed"], 0)
        brief = self.run_async(s.brief.build(with_opener=False))
        self.assertIn("Birthdays this week", brief)
        self.assertIn("today (turns", brief)

    def test_reminders(self):
        s = self.services
        app = create_app(self.settings, s, start_jobs=False)
        with TestClient(app, base_url="http://localhost:8080") as client:
            self.login(client)
            events = self.chat(client, "Remind me to call the garage tomorrow at 9am")
            self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "reminder")
            item = next(e for e in events if e["type"] == "actions")["items"][0]
            self.assertEqual((item["text"], item["status"]), ("call the garage", "scheduled"))
            events = self.chat(client, "remind me to buy milk")
            self.assertIn("When should I remind you", "".join(e.get("text", "") for e in events))
        self.assertIn("call the garage", self.obsidian.files["Jarvis/Reminders.md"])
        s.db.execute("UPDATE scheduled SET due = ? WHERE id = ?", (time.time() - 5, item["id"]))
        self.assertEqual(self.run_async(s.scheduler.run_due())["ran"], 1)
        note = s.db.one("SELECT * FROM notifications WHERE title = 'Reminder'")
        self.assertEqual(note["message"], "call the garage")
        self.assertEqual(s.scheduler.get(item["id"])["status"], "done")
        # ticking an open reminder in Obsidian cancels it
        other = s.scheduler.add_reminder("water the plants every day at 8am")
        self.run_async(s.writer.flush())
        text = self.obsidian.files["Jarvis/Reminders.md"]
        self.assertIn("🔁 every day", text)
        self.obsidian.files["Jarvis/Reminders.md"] = text.replace(f"- [ ] water the plants", "- [x] water the plants")
        self.assertEqual(self.run_async(s.scheduler.sync_ticks())["cancelled"], 1)
        self.assertEqual(s.scheduler.get(other["id"])["status"], "cancelled")

    def test_bible_passages_by_script(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from fakes import Server
        import jarvis.bible as bible
        self.assertEqual(bible.find_reference("Read me Philippians 1"), "philippians 1")
        self.assertEqual(bible.find_reference("read 1 Cor 13:4-7 please"), "1 corinthians 13:4-7")
        self.assertEqual(bible.find_reference("what does psalm 23 say?"), "psalms 23")
        self.assertIsNone(bible.find_reference("Is Mark 3 coming over?"))
        self.assertIsNone(bible.find_reference("turn on the heating at 7"))
        self.assertIsNone(bible.find_reference("what does the bill say, is 3 enough?"))  # not Isaiah 3
        self.assertEqual(bible.find_reference("Ps 23"), "psalms 23")
        asked = []

        async def api(request):
            asked.append((request.path_params["ref"], request.query_params.get("translation")))
            return JSONResponse({"reference": "Philippians 1", "translation_name": "World English Bible, British Edition",
                                 "verses": [{"verse": n, "text": f"Verse {n} text.\n"} for n in range(1, 8)]})
        with Server(Starlette(routes=[Route("/{ref:path}", api)])) as server:
            bible.API = server.url
            try:
                events = [e for e in self.run_async(self._collect(self.services.assistant.handle("Read me Philippians 1")))]
            finally:
                bible.API = "https://bible-api.com"
        meta = next(e for e in events if e["type"] == "meta")
        self.assertEqual((meta["route"], meta.get("speak")), ("bible", True))  # "read me" → spoken
        text = "".join(e.get("text", "") for e in events if e["type"] == "token")
        self.assertIn("**Philippians 1** · World English Bible, British Edition", text)
        self.assertIn("¹ Verse 1 text. ² Verse 2 text.", text)
        self.assertIn("\n\n⁶ Verse 6 text.", text)  # paragraphs of five verses
        self.assertEqual(asked, [("philippians 1", "webbe")])
        # "1" and "read" don't make it a Home Assistant question
        self.assertEqual(self.run_async(self.services.ha.mentioned("Read me Philippians 1")), [])
        self.assertEqual(self.run_async(self.services.ha.mentioned("cylinder temperature"))[0][1],
                         "sensor.cylinder_temperature")  # (Home Assistant is connected in this test)

    @staticmethod
    async def _collect(generator):
        return [e async for e in generator]

    def test_history_is_one_timeline(self):
        s = self.services
        add = lambda sql, *a: s.db.execute(sql, a)  # noqa: E731
        add("INSERT INTO scheduled (created, kind, text, status, last_run, result) VALUES (10, 'reminder', 'call Mum', "
            "'done', 100, 'sent')")
        add("INSERT INTO scheduled (created, kind, text, status, decided) VALUES (20, 'ha', 'Turn off Hall Light', "
            "'cancelled', 150)")
        add("INSERT INTO scheduled (created, kind, text, status, last_run, repeat, result) VALUES (30, 'ha', "
            "'Turn on Coffee Machine', 'scheduled', 300, 'weekdays', 'done')")
        for n, (status, decided, error) in enumerate([("added", 200, ""), ("dismissed", 250, ""),
                                                      ("dismissed", 400, "auto: notice email")]):
            add("INSERT INTO event_proposals (created, source, fingerprint, title, start, end, all_day, status, decided, "
                "error, confidence) VALUES (1, 'llm', ?, ?, '2030-01-01', '2030-01-02', 1, ?, ?, ?, 0.9)",
                f"h{n}", f"Event {n}", status, decided, error)
        history = s.history()
        self.assertEqual([(h["text"], h["status"]) for h in history], [
            ("Turn on Coffee Machine", "done"), ("Event 1", "dismissed"), ("Event 0", "added to calendar"),
            ("Turn off Hall Light", "cancelled"), ("call Mum", "done")])  # newest first; automatic clean-ups hidden
        self.assertIn("repeats weekdays", history[0]["detail"])

    def test_home_actions(self):
        s = self.services
        app = create_app(self.settings, s, start_jobs=False)
        with TestClient(app, base_url="http://localhost:8080") as client:
            self.login(client)
            events = self.chat(client, "turn off the kitchen lights at 11pm")
            item = next(e for e in events if e["type"] == "actions")["items"][0]
            self.assertEqual(item["status"], "proposed")
            self.assertEqual(sorted(item["payload"]["entity_ids"]), ["light.kitchen_ceiling", "light.kitchen_under_cabinet"])
            self.assertEqual(self.home.calls, [], "nothing runs before confirmation")
            confirmed = client.post(f"/api/scheduled/{item['id']}/confirm", headers={"X-Jarvis": "1"}).json()
            self.assertEqual(confirmed["status"], "scheduled")
            # an immediate command runs as soon as it is confirmed
            events = self.chat(client, "set the living room thermostat to 21 degrees")
            now_item = next(e for e in events if e["type"] == "actions")["items"][0]
            client.post(f"/api/scheduled/{now_item['id']}/confirm", headers={"X-Jarvis": "1"})
            self.assertEqual(self.home.calls[-1], ("climate", "set_temperature",
                                                   {"entity_id": "climate.living_room", "temperature": 21.0}))
            text = "".join(e.get("text", "") for e in self.chat(client, "unlock the front door"))
            self.assertIn("won't unlock", text)
            events = self.chat(client, "every weekday at 7am turn on the coffee machine")
            repeat = next(e for e in events if e["type"] == "actions")["items"][0]
            self.assertEqual(repeat["repeat"], "weekdays")
            client.post(f"/api/scheduled/{repeat['id']}/confirm", headers={"X-Jarvis": "1"})
            self.assertEqual(repeat["payload"]["entity_ids"], ["switch.coffee_machine"])  # not its binary_sensor
            # read-only entities (binary_sensor, sensor) are never acted on, and Jarvis says why
            text = "".join(e.get("text", "") for e in self.chat(client, "turn off the hall motion"))
            self.assertIn("can only be read", text)
            self.assertNotIn("actions", [e["type"] for e in self.chat(client, "turn on the hall motion")])
            from jarvis.ha import HAError, parse_command
            for phrase in ("turn on the hall motion", "turn off the coffee machine running", "set hall motion to 5"):
                with self.assertRaises(HAError):
                    self.run_async(s.ha.resolve(parse_command(phrase)))
            action = self.run_async(s.ha.resolve(parse_command("turn on the hall")))
            self.assertEqual(action.entity_ids, ["light.hall"])
            # names taught with "remember …" are used for commands and questions
            text = "".join(e.get("text", "") for e in self.chat(client, "remember the gas water heater is the entity "
                                                                        "water_heater.hot_water"))
            self.assertIn("“gas water heater” now means `water_heater.hot_water`", text)
            events = self.chat(client, "turn on the gas water heater")
            taught = next(e for e in events if e["type"] == "actions")["items"][0]
            self.assertEqual(taught["payload"]["entity_ids"], ["water_heater.hot_water"])
            self.assertEqual(taught["payload"]["service"], "turn_on")
            self.chat(client, "what temperature is the gas water heater at?")
            prompt = self.ollama.requests[-1]["messages"][-1]["content"]
            self.assertTrue(prompt.split("ASKED ABOUT:\n", 1)[1].startswith("Hot Water (water_heater.hot_water)"), prompt)
            text = "".join(e.get("text", "") for e in self.chat(client, "remember the pool pump is switch.pool_pump"))
            self.assertIn("couldn't find `switch.pool_pump`", text)
            self.run_async(s.writer.flush())
            self.assertIn("- gas water heater → `water_heater.hot_water`", self.obsidian.files["Jarvis/Home names.md"])
            # an ID typed slightly differently (thermostat1 vs thermostat_1) is corrected, and unknown ones explained
            text = "".join(e.get("text", "") for e in self.chat(client, "remember the espresso maker is switch.coffeemachine"))
            self.assertIn("I used `switch.coffee_machine`", text)
            s.db.execute("INSERT OR REPLACE INTO ha_aliases (alias, entity_id, source, updated) "
                         "VALUES ('boiler flame', 'switch.coffeemachine', 'chat', 1)")  # e.g. learned from an old capture
            events = self.chat(client, "turn on the boiler flame")
            self.assertEqual(next(e for e in events if e["type"] == "actions")["items"][0]["payload"]["entity_ids"],
                             ["switch.coffee_machine"])
            s.db.execute("INSERT OR REPLACE INTO ha_aliases (alias, entity_id, source, updated) "
                         "VALUES ('pool pump', 'switch.pool_pumpp', 'chat', 1)")
            text = "".join(e.get("text", "") for e in self.chat(client, "turn on the pool pump"))
            self.assertIn("no entity with that ID", text)
            events = self.chat(client, "turn on water_heater.hot_water")
            self.assertEqual(next(e for e in events if e["type"] == "actions")["items"][0]["payload"]["entity_ids"],
                             ["water_heater.hot_water"])
            text = "".join(e.get("text", "") for e in self.chat(client, "turn on binary_sensor.hall_motion"))
            self.assertIn("can only be read", text)
            # captures saved before this feature are learned at startup
            s.db.execute("INSERT INTO captures (ts, day, text) VALUES (1, '2026-09-29', "
                         "'the landing lamp is the entity light.hall')")
            s._learn_home_names_from_captures()
            self.assertEqual(s.db.one("SELECT entity_id FROM ha_aliases WHERE alias = 'landing lamp'")["entity_id"],
                             "light.hall")
            # lines you add to the note yourself count too
            self.obsidian.files["Jarvis/Home names.md"] += "\n- espresso → switch.coffee_machine\n"
            events = self.chat(client, "turn off the espresso")
            self.assertEqual(next(e for e in events if e["type"] == "actions")["items"][0]["payload"]["entity_ids"],
                             ["switch.coffee_machine"])
            # status question uses live states
            # named by script from Home Assistant's own entity names — the router model isn't asked
            before = len(self.ollama.requests)
            events = self.chat(client, "what is the cylinder temperature in my home?")
            self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "home")
            self.assertEqual([r for r in self.ollama.requests[before:] if r.get("format") == "json"], [])
            prompt = self.ollama.requests[-1]["messages"][-1]["content"]
            asked = prompt.split("ASKED ABOUT:\n", 1)[1].split("\n\n", 1)[0]
            self.assertTrue(asked.startswith("Cylinder Temperature (sensor.cylinder_temperature): 52.5 °C"), prompt)
            self.assertIn("Never give a different sensor", self.ollama.requests[-1]["messages"][0]["content"])
            # unavailable sensor: flagged, with the last reading from history and its sibling sensors
            events = self.chat(client, "What is the boiler temp sensors cylinder temperature?")
            prompt = self.ollama.requests[-1]["messages"][-1]["content"]
            asked = prompt.split("ASKED ABOUT:\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("sensor.boiler_temp_sensors_cylinder_temperature", asked)
            self.assertIn("NO CURRENT READING", asked)
            self.assertIn("last reading 48.2 °C at 2026-09-29 17:02", asked)
            self.assertIn("SAME DEVICE:\nBoiler-Temp-Sensors Flow Temperature", prompt)
            events = self.chat(client, "how hot is the hot water?")
            prompt = self.ollama.requests[-1]["messages"][-1]["content"]
            self.assertIn("current_temperature=52", prompt)
            events = self.chat(client, "is the garage door open?")
            self.assertEqual(next(e for e in events if e["type"] == "meta")["route"], "home")
            prompt = self.ollama.requests[-1]["messages"][-1]["content"]
            self.assertIn("Garage Door (cover.garage_door): closed", prompt)
        # the timed kitchen action fires when due
        s.db.execute("UPDATE scheduled SET due = ? WHERE id = ?", (time.time() - 5, item["id"]))
        s.db.execute("UPDATE scheduled SET due = ? WHERE id = ?", (time.time() - 5, repeat["id"]))
        self.assertEqual(self.run_async(s.scheduler.run_due())["ran"], 2)
        self.assertIn(("light", "turn_off", {"entity_id": ["light.kitchen_ceiling", "light.kitchen_under_cabinet"]}),
                      self.home.calls)
        again = s.scheduler.get(repeat["id"])
        self.assertEqual(again["status"], "scheduled", "repeating actions are rescheduled")
        self.assertGreater(again["due"], time.time())
        # an action missed by more than 30 minutes is not run late
        s.db.execute("UPDATE scheduled SET due = ? WHERE id = ?", (time.time() - 7200, repeat["id"]))
        calls = len(self.home.calls)
        self.run_async(s.scheduler.run_due())
        self.assertEqual(len(self.home.calls), calls)
        self.assertIn("missed", s.scheduler.get(repeat["id"])["result"])

    def test_brief(self):
        s = self.services
        self.settings.brief_ha_entities = ["sensor.bin_collection"]
        now = datetime.now(TZ)
        start = (now.replace(hour=23, minute=0, second=0, microsecond=0)).isoformat()
        s.db.execute("INSERT INTO events (event_id, calendar_id, path, summary, start, end, all_day, location, description, "
                     "attendees, status, updated, html_link) VALUES ('e1','primary','Sources/Calendar/x.md','Late film',?,?,0,"
                     "'Cinema','', '[]','confirmed','1','')", (start, start))
        s.gmail_pipeline.gmail = FakeGmail([gmail_message("q1", "tq1", "Sam Jones <sam@example.com>", "Dinner",
                                                          "Are you free for dinner on Saturday?")])
        self.run_async(s.writer.refresh_people())
        self.run_async(s.gmail_pipeline.run())
        s.scheduler.add_reminder("put the bins out tonight")
        text = self.run_async(s.brief.build(with_opener=False))
        for expected in ("Good morning", "Late film", "Email that may need a reply", "Sam Jones — [[Sources/Email",
                         "Bin collection: Recycling"):
            self.assertIn(expected, text)
        if now.hour < 20:
            self.assertIn("put the bins out", text)
        self.assertEqual(self.run_async(s.brief.run(force=True)), "sent")
        self.run_async(s.writer.flush())
        journal = self.obsidian.files[f"Journal/{now:%Y}/{now:%Y-%m-%d}.md"]
        self.assertIn("## Morning brief", journal)
        self.assertTrue(s.db.one("SELECT 1 FROM chat_messages WHERE role = 'activity' AND content LIKE '%Good morning%'"))
