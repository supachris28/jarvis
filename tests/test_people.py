"""People and families: linked both ways, birthdays, prompts for regular contacts, before-you-meet cards."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from starlette.testclient import TestClient

from jarvis.auth import Auth
from jarvis.vault.markdown import split_frontmatter
from jarvis.web.app import create_app
from test_jarvis import IntegrationBase


class PeopleTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        s = self.services
        self.obsidian.files["People/Ben Topliss.md"] = "---\nemails: [ben@example.com]\n---\n# Ben Topliss\n"
        self.obsidian.files["People/Emily Topliss.md"] = "---\nemails: [emily@example.com]\n---\n# Emily Topliss\n"
        s.db.execute("INSERT OR REPLACE INTO people (email, name, path) VALUES ('ben@example.com', 'Ben Topliss', "
                     "'People/Ben Topliss.md'), ('emily@example.com', 'Emily Topliss', 'People/Emily Topliss.md')")
        now = time.time()
        for n in range(4):   # Ben is a regular: emails both ways
            s.db.execute("INSERT INTO emails (message_id, thread_id, ts, from_addr, from_name, to_addrs, subject, labels, "
                         "bulk, outgoing, snippet, body, attachments) VALUES (?, ?, ?, ?, '', ?, ?, '[]', 0, ?, '', '', '[]')",
                         (f"m{n}", f"t{n}", now - n * 86400, "ben@example.com" if n % 2 else "chris@example.com",
                          json.dumps([["ben@example.com", "Ben"]] if not n % 2 else [["chris@example.com", "Chris"]]),
                          f"Weekend plans {n}", 0 if n % 2 else 1))

    def props(self, path):
        return split_frontmatter(self.obsidian.files[path])[0]

    def test_families_linked_both_ways(self):
        s = self.services
        directory = self.run_async(s.people.directory())
        ben = next(p for p in directory["people"] if p["name"] == "Ben Topliss")
        self.assertEqual(ben["contact_count"], 4)
        self.assertEqual([p["name"] for p in directory["prompts"]], ["Ben Topliss"], "a regular Jarvis knows little about")
        person = self.run_async(s.people.update("People/Ben Topliss.md", {
            "relation": "friend from church", "birthday": "12 March 1986", "family": "Topliss",
            "partner": "Emily", "children": "Sam, Lily"}))
        self.assertEqual(person["relation"], "friend from church")
        ben = self.props("People/Ben Topliss.md")
        self.assertEqual((ben["birthday"], ben["family"]), ("1986-03-12", "Topliss"))
        self.assertEqual(ben["partner"], "[[People/Emily Topliss]]")
        self.assertEqual(ben["children"], ["[[People/Sam Topliss]]", "[[People/Lily Topliss]]"])
        emily = self.props("People/Emily Topliss.md")
        self.assertEqual((emily["partner"], emily["family"]), ("[[People/Ben Topliss]]", "Topliss"))
        sam = self.props("People/Sam Topliss.md")   # a new note for a child without an email
        self.assertEqual((sam["parents"], sam["family"]), (["[[People/Ben Topliss]]"], "Topliss"))
        # Emily adds the children too: no duplicates on either side
        self.run_async(s.people.update("People/Emily Topliss.md", {"children": "Sam"}))
        self.assertEqual(len(self.props("People/Sam Topliss.md")["parents"]), 2)
        self.run_async(s.people.update("People/Emily Topliss.md", {"children": "Sam"}))
        self.assertEqual(len(self.props("People/Emily Topliss.md")["children"]), 1)
        directory = self.run_async(s.people.directory())
        family = next(f for f in directory["families"] if f["name"] == "Topliss")
        self.assertEqual(sorted(m["name"] for m in family["members"]),
                         ["Ben Topliss", "Emily Topliss", "Lily Topliss", "Sam Topliss"])
        self.assertEqual(directory["prompts"], [], "nothing left to ask about Ben")
        # every change can be undone from the Vault tab
        self.assertTrue(s.db.one("SELECT 1 FROM note_history WHERE path = 'People/Ben Topliss.md'"))

    def test_partner_children_suggested(self):
        s = self.services
        self.run_async(s.people.update("People/Ben Topliss.md", {"family": "Topliss", "children": "Sam, Lily"}))
        self.run_async(s.people.update("People/Ben Topliss.md", {"partner": "Emily Topliss"}))
        emily = self.run_async(s.people.person("People/Emily Topliss.md"))
        [g] = emily["suggestions"]
        self.assertEqual((g["target"], [c["name"] for c in g["children"]]),
                         ("People/Emily Topliss.md", ["Sam Topliss", "Lily Topliss"]))
        self.assertEqual(g["text"], "Ben has Sam and Lily. Are they Emily's children too?")
        ben = self.run_async(s.people.person("People/Ben Topliss.md"))
        self.assertEqual([g["target"] for g in ben["suggestions"]], ["People/Emily Topliss.md"], "shown on Ben's page too")
        sam = self.run_async(s.people.person("People/Sam Topliss.md"))
        [g] = sam["suggestions"]
        self.assertEqual((g["target"], [c["name"] for c in g["children"]]), ("People/Emily Topliss.md", ["Sam Topliss"]))
        self.assertEqual(g["text"], "Ben has Sam. Is Sam Emily's child too?")
        # Lily is a step-child: no; Sam: yes
        s.people.dismiss_children("People/Emily Topliss.md", ["People/Lily Topliss.md"])
        self.run_async(s.people.update("People/Emily Topliss.md", {"children": ["Sam Topliss"]}))
        self.assertEqual(self.props("People/Sam Topliss.md")["parents"],
                         ["[[People/Ben Topliss]]", "[[People/Emily Topliss]]"])
        for page in ("Emily", "Ben", "Sam", "Lily"):
            path = f"People/{page} Topliss.md"
            self.assertEqual(self.run_async(s.people.person(path))["suggestions"], [], page)

    def test_api_and_skipping(self):
        app = create_app(self.settings, self.services, start_jobs=False)
        Auth(self.services.db).set_password("a very long password")
        h = {"X-Jarvis": "1"}
        with TestClient(app, base_url="http://localhost:8080") as client:
            client.post("/api/login", json={"password": "a very long password"}, headers=h)
            self.assertEqual(len(client.get("/api/people").json()["prompts"]), 1)
            client.post("/api/people", json={"path": "People/Ben Topliss.md", "skip": True}, headers=h)
            self.assertEqual(client.get("/api/people").json()["prompts"], [])
            created = client.post("/api/people", json={"name": "Grace Hopper", "relation": "neighbour"}, headers=h).json()
            self.assertEqual((created["path"], created["relation"]), ("People/Grace Hopper.md", "neighbour"))
            one = client.get("/api/people/one", params={"path": "People/Ben Topliss.md"}).json()
            self.assertEqual(one["emails"][0]["subject"], "Weekend plans 0")
            self.assertEqual(client.get("/api/people/one", params={"path": "../x.md"}).status_code, 404)
            # dates typed in Obsidian are YAML dates, not strings
            self.obsidian.files["People/Ada Lovelace.md"] = ("---\nbirthday: 1985-12-10\nmet: 2020-01-02 10:30:00\n"
                                                             "aliases: [Ada]\n---\n# Ada\n")
            ada = client.get("/api/people/one", params={"path": "People/Ada Lovelace.md"})
            self.assertEqual(ada.status_code, 200)
            self.assertEqual(ada.json()["properties"]["birthday"], "1985-12-10")
            self.assertEqual(client.get("/api/people").status_code, 200)
            self.assertEqual(client.post("/api/people", json={"path": "Notes/x.md", "relation": "x"}, headers=h).status_code, 400)
            log = client.get("/api/changelog").json()
            self.assertTrue(log["markdown"].startswith("# What's new in Jarvis"))
            self.assertIn(f"## {log['version']}", log["markdown"], "the changelog has an entry for this version")
        # the Nextcloud/files vault parses YAML itself, so dates arrive as dates
        from jarvis.pipelines.people import plain
        from datetime import date
        self.assertEqual(plain({"birthday": date(1985, 12, 10), "kids": [{"born": date(2015, 1, 2)}]}),
                         {"birthday": "1985-12-10", "kids": [{"born": "2015-01-02"}]})

    def test_before_you_meet(self):
        s = self.services
        self.run_async(s.people.update("People/Ben Topliss.md", {"relation": "friend", "partner": "Emily"}))
        s.tasks.add("Return Ben's drill")
        start = datetime.now(self.settings.tz) + timedelta(minutes=90)
        s.db.execute("INSERT INTO events (event_id, calendar_id, path, summary, start, end, all_day, location, description, "
                     "attendees, status, updated, html_link) VALUES ('z1', 'primary', 'x.md', 'Zoom with Ben', ?, ?, 0, '', '',"
                     " '[]', 'confirmed', '1', '')", (start.isoformat(), (start + timedelta(hours=1)).isoformat()))
        self.run_async(s.writer.refresh_people())
        self.assertEqual(self.run_async(s.people.meeting_prep(s.notifier)), 1)
        row = s.db.one("SELECT title, message FROM notifications WHERE title LIKE 'Before %'")
        self.assertEqual(row["title"], f"Before Zoom with Ben ({start:%H:%M})")
        self.assertIn("**Ben Topliss** — friend", row["message"])
        self.assertIn("Family: Emily Topliss", row["message"])
        self.assertIn("Your to-dos: Return Ben's drill", row["message"])
        self.assertEqual(self.run_async(s.people.meeting_prep(s.notifier)), 0, "once per event")


class MeetingTests(PeopleTests):
    def test_plan_shows_meetings_with_their_people(self):
        from jarvis.pipelines.people import upcoming_meetings
        s = self.services
        s.db.execute("UPDATE emails SET from_name = 'Chris Key' WHERE outgoing = 1")
        tz = self.settings.tz
        day = datetime.now(tz).date() + timedelta(days=2)

        def event(eid, title, hour, attendees="[]"):
            start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            s.db.execute("INSERT INTO events (event_id, calendar_id, path, summary, start, end, all_day, location, "
                         "description, attendees, status, updated, html_link) VALUES (?, 'primary', 'x.md', ?, ?, ?, 0, "
                         "'', '', ?, 'confirmed', '1', '')", (eid, title, start.isoformat(),
                                                             (start + timedelta(hours=1)).isoformat(), attendees))
        event("e1", "Chris+Phil+Casper🐕", 7)
        event("e2", "Wine and Zoom with Ben", 20)
        event("e3", "Prayer meeting", 19, '[{"email": "emily@example.com", "name": "Emily"}, '
                                          '{"email": "new@example.com", "name": "Grace Hopper"}]')
        meetings = {m["event_id"]: m for m in self.run_async(upcoming_meetings(s.people))}
        self.assertEqual([u["name"] for u in meetings["e1"]["unknown"]], ["Phil", "Casper"], "not Chris (that's you)")
        self.assertEqual([p["name"] for p in meetings["e2"]["people"]], ["Ben Topliss"])
        self.assertEqual(meetings["e2"]["people"][0]["missing"], ["how you know them", "birthday", "family"])
        self.assertEqual([p["name"] for p in meetings["e3"]["people"]], ["Emily Topliss"])
        self.assertEqual(meetings["e3"]["unknown"], [{"name": "Grace Hopper", "email": "new@example.com"}])
        # Phil is a new person, Casper is the dog; 'Benny' becomes one of Ben's names
        created = self.run_async(s.people.link_name("Phil", create=True))
        self.assertEqual(created["path"], "People/Phil.md")
        self.run_async(s.people.link_name("Casper", not_person=True))
        meetings = {m["event_id"]: m for m in self.run_async(upcoming_meetings(s.people))}
        self.assertEqual([p["name"] for p in meetings["e1"]["people"]], ["Phil"])
        self.assertEqual(meetings["e1"]["unknown"], [])
        event("e4", "Coffee with Benny", 11)
        self.assertEqual([u["name"] for u in {m["event_id"]: m for m in self.run_async(upcoming_meetings(s.people))}["e4"]["unknown"]], ["Benny"])
        self.run_async(s.people.link_name("Benny", "People/Ben Topliss.md"))
        self.assertIn("Benny", self.props("People/Ben Topliss.md")["aliases"])
        meetings = {m["event_id"]: m for m in self.run_async(upcoming_meetings(s.people))}
        self.assertEqual([p["name"] for p in meetings["e4"]["people"]], ["Ben Topliss"])
