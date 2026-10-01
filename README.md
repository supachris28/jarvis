# Jarvis

Jarvis is a self-hosted personal assistant. It runs as a web app (installable on phone and PC) in a container on the home server at **https://jarvis.keeeys.uk**.

- **Knowledge store:** an Obsidian vault kept as plain files in **Nextcloud** on the same server. Jarvis indexes the files itself (search, #tags, frontmatter, backlinks) and writes through Nextcloud, so your devices sync every change.
- **The PC is only needed for Ollama** (GPU), which does conversation and summaries.

## What works now (v0.7)

- **Chat** with the local model, streamed to the browser. Each answer is routed automatically to one of these sources:
  - your vault (people, #tags, backlinks, full-text search). A question naming someone with a note under `People/` goes straight to the vault, whatever subfolder the note is in, and matches the note's title or its `aliases:`. These questions never go to the router model or the web
  - Gmail
  - Google Calendar
  - Google Drive
  - Home Assistant. A question that names a device, room or sensor ("cylinder temperature", "garage door") is matched by script against your entity names, so it goes straight there. Readings kept in attributes, such as a water heater's current temperature, are included
  - the internet: general questions are searched through a private SearXNG, the top pages are read, and the answer cites its sources
  - if the model doesn't know, it searches online instead of saying so. It can ask for a search itself, and a script also spots "I don't know" style answers. Questions about your own things ("my …", "our …") are never sent to a search engine
- **Birthdays** are gathered by script from:
  - Google Contacts
  - People notes: a `birthday:` (or `born:`, `dob:`) property, or a "Birthday: 12 March" line
  - mentions anywhere in the vault, such as "Jen's birthday is 3 May"

  Most date formats are understood. Jarvis works out the next date and the age, and adds the relation when the note has a `relation:` property. The same list feeds the morning brief.
- **Looks everywhere.** When the first place Jarvis checks has nothing, or the answer says it doesn't know, the model chooses what to search for in your notes, email, calendar, Drive and, for public facts only, the web. All of those are searched at once, and the answer says which place each fact came from.
- **"Remember that …"** stores the text in `Inbox/<date> Captures` and the day's journal, and links the names of people it already knows. No AI is used for this.
- **Background ingestion** is done by scripts, with no AI:
  - Gmail and Calendar are checked on a timer.
  - Non-bulk email threads and all events are written to `Sources/`.
  - Correspondents and attendees get notes in `People/`, each with a timeline section Jarvis keeps up to date.
  - Every day gets a `Journal/` log.
  - If Nextcloud can't be reached, writes are queued until it's back.
- **Events from email → Google Calendar.** Invitations and booking details are read by scripts; other emails are read by the local model within a daily limit. Each event found becomes an *Add to calendar?* card (Events tab, chat and ntfy), and nothing is added until you confirm. You can also say "add dentist Friday 3pm to my calendar".
  - Notices are skipped by script (terms and conditions, policy or price changes, offers, statements). Automated senders only reach the model when the subject looks like a booking, appointment or delivery, and need a surer answer from it.
  - **Not from this sender** on a card stops suggestions from that address. A sender is also muted automatically after two dismissals with nothing accepted. Muted senders are listed at the bottom of the Plan tab, where you can unmute them.
- **Save reports.** Every vault write shows up in the chat as a *Saved to your vault* list of each note and what went into it, plus a quiet ntfy summary.
- **Morning brief** every day at a set time: weather, today's calendar, reminders, anything waiting for your OK, email that may need a reply, birthdays and home sensors. It goes to ntfy, the chat and the journal.
- **Reminders** ("remind me to … tomorrow at 9am", including repeats) and **Home Assistant** actions ("turn off the kitchen lights at 11pm"). Home actions need your confirmation and can be timed or repeating. Everything open is mirrored to `Jarvis/Reminders.md`.
- **People from Google Contacts**: phones, birthdays and relations are merged into `People/` notes.
- **Spoken replies** with Kokoro-82M (a local container), ElevenLabs, or the device's own voice.
- **Notifications** through ntfy for VIP senders, keywords, important email from known people, and upcoming events. Quiet hours and a rate cap stop them getting noisy.
- **Diagnostics:** a **Logs** tab groups everything into runs (chat, jobs, actions). Tap a run to see each step with timings, data and errors. **🔍 Details** under any chat reply shows what Jarvis did for it. **Verbose** mode records full detail for an hour, and logs can be downloaded as JSON.
- **Safety:**
  - Sign-in needs a password plus an authenticator code.
  - Every change Jarvis makes to the vault is logged and can be reverted from the **Vault** tab.
  - Email and calendar content is treated as data, never as instructions.
  - Actions outside the vault need your confirmation: adding a calendar event is a proposal you approve, and home actions wait for your Confirm, and unlocking doors or disarming alarms is refused.

Deployment steps are in **[docs/DEPLOY.md](docs/DEPLOY.md)**.

## Layout

```text
src/jarvis/
  config.py  db.py  auth.py  llm.py  mcp.py  notify.py  services.py  admin.py  diag.py (logs + traces)
  vault/      files.py (vault folder + SQLite index), webdav.py (writes via Nextcloud), writer.py (outbox, renderers),
              markdown.py (frontmatter + managed blocks), client.py (optional Obsidian REST backend)
  google/     oauth.py (web client), gmail.py, calendar.py
  pipelines/  gmail.py, calendar.py, events.py, contacts.py, scheduled.py, brief.py
  tts.py      spoken replies
  websearch.py internet lookups (SearXNG / Brave, safe page fetch, passage selection)
  ha.py       Home Assistant client, entity matching, command parsing
  extract/    events.py (ICS, JSON-LD, model output validation), when.py (time parsing)
  assistant/  planner.py, core.py
  web/        app.py (Starlette API), static/ (PWA)
deploy/       docker-compose.yml, .env.server.example, swag/*.subdomain.conf, searxng/settings.yml
scripts/      build-push.ps1 / build-push.sh, test.ps1
tests/        unit + integration tests with fake Obsidian and Ollama servers
```

## Develop and test

```sh
pip install -r requirements.txt
PYTHONPATH=src:tests python -m unittest discover -s tests -v      # or .\scripts\test.ps1 on Windows
```

To run locally against your PC, set the variables from `deploy/.env.server.example` (with `JARVIS_PUBLIC_URL=http://localhost:8080`, `JARVIS_SECURE_COOKIES=false` and `JARVIS_DATA_DIR=.jarvis/data`). Then run:

```sh
PYTHONPATH=src python -m jarvis.web.app
```

## Design docs

- [Architecture](docs/ARCHITECTURE.md)
- [Knowledge and learning](docs/KNOWLEDGE_AND_LEARNING.md)
- [Capabilities](docs/CAPABILITIES.md)
- [Project vision](docs/PROJECT_VISION.md)

The original terminal prototype (`jarvis.py`, `gmail_api.py`, `google_mcp.py`, `intelligence.py`, `voice.py`) is superseded by `src/jarvis` and kept only for reference.
