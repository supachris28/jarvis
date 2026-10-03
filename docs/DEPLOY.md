# Deploying Jarvis

Jarvis runs as a container on the home server behind SWAG at **https://jarvis.keeeys.uk**.

- **The Obsidian vault** lives in your **Nextcloud** on the same server. Jarvis reads it from disk and writes through Nextcloud.
- **The PC is only needed for Ollama** (GPU), which answers chat messages and writes summaries.
- **Notifications** go through an **ntfy** container, and internet searches through a **SearXNG** container.

```text
phone / PC browser ──HTTPS──► SWAG ──► jarvis:8080 ──LAN──► PC: Ollama :11434
                                         ├── reads ──► vault folder (Nextcloud data, read-only mount)
                                         ├── writes ─► Nextcloud WebDAV ──sync──► Obsidian on PC / phone
                                         └──► ntfy:80 ──push──► phone
```

When the PC is off, everything except the model keeps working: vault reads and writes, email and calendar checks, reminders, home actions, the morning brief and notifications. Chat answers need the PC. Until it's back, you get raw lookup results instead.

Replace `192.168.68.60` (PC) and `192.168.68.203` (server) below with your real addresses. Give the PC a DHCP reservation so its address doesn't change.

---

## 1. PC: Ollama

1. Make Ollama listen on the LAN. In PowerShell:

   ```powershell
   setx OLLAMA_HOST "0.0.0.0:11434"
   ```

   Then quit Ollama from the system tray and start it again.
2. Pull the model set in `JARVIS_MODEL`:

   ```powershell
   ollama pull llama3.2:3b
   ```

   Your GPU can probably run something larger (for example `qwen2.5:7b` or `qwen2.5:14b`) for better answers. Set `JARVIS_MODEL` to match whatever you pull.
3. Allow only the server through the Windows firewall. Run PowerShell as Administrator:

   ```powershell
   New-NetFirewallRule -DisplayName "Jarvis: Ollama from server" -Direction Inbound -Protocol TCP `
     -LocalPort 11434 -RemoteAddress 192.168.68.203 -Action Allow
   ```

   Ollama has no authentication. Check *Windows Defender Firewall → Inbound rules* for any broader "ollama" rule that Windows created automatically, and disable it.

## 2. The vault in Nextcloud (on the server)

The Obsidian vault is a folder in your Nextcloud files, for example `Obsidian/Jarvis`.

- **Jarvis reads the folder directly from disk.** It keeps its own index of search text, tags, frontmatter and links, so no Obsidian app has to run anywhere.
- **Jarvis writes through Nextcloud's WebDAV API.** Nextcloud sees each change the moment it happens, syncs it to your devices straight away, and keeps a version history of every note.

1. **Create the folder.** In Nextcloud, create `Obsidian/Jarvis`.
2. **Move your existing vault in.** Copy the contents of `D:\Obsidian\Jarvis` into that folder, using the Nextcloud desktop client or the web upload. Keep the old folder as a backup until you're happy.
3. **Create an app password for Jarvis.** In Nextcloud, go to *Personal settings → Security → Devices & sessions*, create an app password named "Jarvis", and put it in `NEXTCLOUD_APP_PASSWORD`.
4. **Find the folder on the server's disk.** It sits inside Nextcloud's data directory, at `<data dir>/<your user>/files/Obsidian/Jarvis`. To find the data directory:

   ```sh
   docker inspect nextcloud --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
   ```

   - **Official image:** the data directory is mounted at `/var/www/html/data` (or `/var/www/html`, with `data/` inside).
   - **linuxserver/nextcloud:** it's mounted at `/data`.

   Put the host path of the vault folder in `VAULT_HOST_PATH`.
5. **Let Jarvis read it.** Nextcloud's files belong to its web server user. Set `VAULT_GID` to that group:
   - `33` (www-data) for the official image;
   - your `PGID` for linuxserver/nextcloud.

   Check it with `stat -c '%g' <VAULT_HOST_PATH>`. Jarvis only needs read access, because it writes through Nextcloud.
6. **Check `NEXTCLOUD_URL`.** It must be a URL Jarvis can reach that's listed in Nextcloud's `trusted_domains`. Your public URL (for example `https://nextcloud.keeeys.uk`) is simplest.
7. **Open the vault on your devices:**
   - **PC:** let the Nextcloud desktop client sync `Obsidian/Jarvis`, then in Obsidian choose *Open folder as vault* on the synced folder. The Local REST API plugin and the firewall rule for port 27124 are no longer needed.
   - **Phone:** Obsidian mobile can sync the same folder with the *Remotely Save* plugin over WebDAV, pointed at `https://nextcloud.keeeys.uk/remote.php/dav/files/<user>/Obsidian/Jarvis`. Otherwise you can simply browse the notes in the Nextcloud app.
8. **Create `Jarvis/Me.md` in the vault.** Write a few lines about you, your family and your preferences. Jarvis includes this note in every conversation.

Your own edits, from Obsidian on any device or from Nextcloud, are picked up by Jarvis's index within a minute.

## 3. Google Cloud (Web application OAuth client)

Do this in the same Google Cloud project you already use.

1. Enable these APIs: **Gmail API**, **Google Calendar API**, **Google Drive API** and **People API**.
2. Create an OAuth client under *APIs & Services → Credentials → Create credentials → OAuth client ID*:
   - Type: **Web application**.
   - Authorised redirect URI: `https://jarvis.keeeys.uk/auth/google/callback`
   - Copy the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
3. On the consent screen (*Google Auth Platform → Data access*), add these scopes. All of them are read-only except `calendar.events`:
   - `gmail.readonly`
   - `calendar.calendarlist.readonly`
   - `calendar.events` (lets Jarvis add events, only after you confirm each one)
   - `drive.readonly`
   - `contacts.readonly`
4. Publish the app: *Google Auth Platform → Audience → Publish app*.
   - While the app is in *Testing*, Google expires the refresh token after 7 days and background checks stop.
   - You'll see an "unverified app" warning when you sign in. That's expected for a personal app: choose *Advanced → Go to Jarvis*.

## 4. PC: build the image and push it to your registry

You need Docker Desktop on the PC.

1. Sign in to your registry once. Docker stores the credentials, so they never need to go in a file or in this repo:

   ```powershell
   docker login registry.keeeys.uk
   ```

2. From the repo root (`D:\Dev\jarvis`):

   ```powershell
   .\scripts\build-push.ps1
   ```

   This pushes `registry.keeeys.uk/jarvis:latest` plus a date tag. If the server is ARM, add `-Platform linux/arm64`.

## 5. Server: set up the stack

1. Copy the repo's `deploy/` folder to the server, for example `/opt/jarvis`. The folder should contain:

   ```text
   /opt/jarvis/docker-compose.yml
   /opt/jarvis/.env.server.example
   /opt/jarvis/swag/…
   /opt/jarvis/searxng/settings.yml
   ```

2. Create the settings file, then fill in the IPs, keys and Google client values:

   ```sh
   cd /opt/jarvis
   cp .env.server.example .env && chmod 600 .env
   nano .env
   ```

3. *(Old Obsidian-on-PC setup only; skip otherwise.)* Add the Obsidian certificate:

   ```sh
   mkdir -p certs && cp /path/to/obsidian-local-rest-api.crt certs/obsidian-ca.crt
   ```

4. Find SWAG's Docker network and put its name in `SWAG_NETWORK` in `.env`:

   ```sh
   docker inspect swag --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
   ```

5. Configure SWAG:
   1. Add `jarvis` and `ntfy` to SWAG's `SUBDOMAINS`, unless you use a wildcard certificate, and create DNS records for `jarvis.keeeys.uk` and `ntfy.keeeys.uk`.
   2. Copy the proxy configs and restart SWAG:

      ```sh
      cp swag/jarvis.subdomain.conf swag/ntfy.subdomain.conf <swag-config>/nginx/proxy-confs/
      docker restart swag
      ```

6. Start the stack:

   ```sh
   docker login registry.keeeys.uk
   docker compose pull
   docker compose up -d
   docker compose logs -f jarvis      # Ctrl+C to stop following
   ```

7. Check that Jarvis can reach the PC and see the vault:

   ```sh
   curl http://192.168.68.60:11434/api/tags
   docker exec jarvis ls /vault | head      # should list your vault's folders
   ```

8. Set your Jarvis login:

   ```sh
   docker exec -it jarvis python -m jarvis.admin set-password
   docker exec -it jarvis python -m jarvis.admin totp-setup
   ```

   `totp-setup` prints a key. Add it to your authenticator app as a time-based code. Two-factor sign-in is strongly recommended because Jarvis is on the internet.

9. Set up ntfy. The username `chris` below is only an example:

   ```sh
   docker exec -it ntfy ntfy user add --role=admin chris
   docker exec ntfy ntfy token add chris
   ```

   `ntfy token add` prints a token. Put it in `.env` as `NTFY_TOKEN`, then restart Jarvis:

   ```sh
   docker compose up -d
   ```

## 6. First use

1. Open **https://jarvis.keeeys.uk** and sign in.
2. Go to **Status → Connect Google** and approve access. This includes adding calendar events, which Jarvis only does after you confirm each one.
   - The first email and calendar run imports the last `INGEST_BACKFILL_DAYS` (30 by default). Bulk and promotional email is counted but not stored.
   - Once it finishes, notes appear in the vault under `Sources/`, `People/` and `Journal/`.
3. On **Status**, check that all the cards are green, then press **Send test notification**.
4. Try some messages:
   - "remember that Sam's birthday is 12 April"
   - "what do my notes say about Sam?"
   - "notes tagged #holiday"
   - "what's on tomorrow?"
   - "check my emails for events I need to add to my calendar"
   - "add dentist on Friday at 3pm to my calendar"

### Events from email

- Jarvis looks for events in three ways, cheapest first:
  1. Calendar invitations (`.ics`), read by a script.
  2. Booking details that restaurants, airlines and ticket sites embed in their emails (schema.org), read by a script.
  3. For other emails that mention both a date and an event word, the model on your PC reads the email. This is capped at `EVENTS_LLM_DAILY_LIMIT` emails a day, and waits while the PC is off.
- Each event found sends an ntfy alert, *Add to calendar? …*. Nothing is added until you tap **Add to calendar**, either in the **Events** tab or in the chat. You can **Edit** the title, time or place first.
- Events already in your calendar, including invites Gmail added itself, are skipped.
- To have exact calendar invitations added without asking, set `EVENTS_AUTO_ADD=ics`.

### What was saved

- Every time Jarvis writes to the vault, a **Saved to your vault** entry appears in the chat, listing each note (new or updated) and what went into it.
- "Remember that …" replies quote exactly what was saved.
- ntfy also sends a quiet summary, at most every `NOTIFY_VAULT_SAVES_MINUTES`. Set `NOTIFY_VAULT_SAVES=off` if you only want the in-app list.

### Morning brief

- Every morning at `BRIEF_TIME`, Jarvis sends an ntfy notification, posts the full brief in the chat and adds it to the day's journal note in Obsidian.
- Every evening at `EVENING_TIME` (default 21:00, `off` to disable) it sends a preview of tomorrow: calendar (flagging an early start), booking references and things to bring from event descriptions, reminders, parcels expected, birthdays, weather and the brief's Home Assistant readings. Ask "evening preview" in chat to see it any time.

### Home Assistant triggers

Status → **Home Assistant triggers** shows a token and an example `rest_command` + automations. Home Assistant can
POST `/api/hook/morning` (send today's brief now — e.g. first motion downstairs; `BRIEF_TIME` stays as the
fallback, once a day), `/api/hook/home` ("Welcome home": the rest of today, parcels delivered or to collect, things
due by tomorrow, an early start) and `/api/hook/evening` (the evening preview now). Each replies with a short
`speech` text to play with `tts.speak`. The token goes in an `Authorization: Bearer …` header; "New token" replaces it.

### Shared screenshots, birthdays, adding to notes

- Share a screenshot (a booking, a WhatsApp message) to Jarvis: the server reads its text (Tesseract, in the
  image) and offers Add to calendar / Remember / Track / Ask. Text shares work as before.
- A week before each birthday (and the day before) you get a heads-up with gift ideas from the person's note —
  lines that mention gifts, wish lists or what they like, or anything under a "Gift ideas" heading
  ("Birthday heads-up" in the notification settings).
- In Jarvis's note reader, "Add to this note" appends a dated line to the note; Undo there or in the Vault tab.

### Speaking to Jarvis (🎤)

The 🎤 button next to Send records what you say, sends it when you pause (or tap ■), and reads the reply aloud.
Out of the box it uses the phone's own speech recognition (in Chrome that audio goes to Google). For private, more
accurate recognition run Whisper on the PC's GPU next to Ollama, e.g. [speaches](https://speaches.ai):

```
docker run -d --name whisper --gpus all -p 8000:8000 --restart unless-stopped \
  -v whisper-cache:/home/ubuntu/.cache/huggingface ghcr.io/speaches-ai/speaches:latest-cuda
curl -X POST http://localhost:8000/v1/models/Systran/faster-whisper-small.en   # download the model once
```

then set `JARVIS_STT_URL=http://<PC IP>:8000` (allow port 8000 through the PC's firewall, as for Ollama). When the
PC is off Jarvis falls back to the phone's recognition. Status shows which is in use.

### Renewals, deadlines and replies you're waiting for

Jarvis reads renewal notices (insurance, subscriptions, memberships, licences), MOT reminders, "return by" dates,
trial endings and payment due dates from your email — by script, no model — and reminds you ahead of each: two
weeks for renewals, three for an MOT, a few days for returns, trials and payments ("Renewals and deadlines" in the
notification settings). The first time it runs it reads the past year of email and sends one summary. They're
listed in Plan, the morning brief (next fortnight) and the evening preview (due tomorrow); mark one Done or
"Not this". Emails you sent that ask something and have had no reply after `FOLLOWUP_DAYS` (default 3) days are
listed under "Waiting on a reply" (a notification is available, off by default). Ask "any renewals coming up?" or
"who hasn't replied?" in chat.

### Backups

Every night at `BACKUP_TIME` (default 03:15, `off` to disable) Jarvis copies its database — reminders, deliveries,
chat, settings, history, reported answers — while running, compresses it, keeps the last 7 in `/data/backups` and,
when `NEXTCLOUD_*` is set, uploads it to the Nextcloud folder `BACKUP_DIR` (default `Backups/Jarvis`; never inside
the vault). Nextcloud keeps one per weekday plus one per month. Sign-in sessions and the 2FA secret are left out;
the Google token file is not included. Status → Background jobs → backup → Run makes one now.

To restore: stop the container, `gunzip -c jarvis-Mon.sqlite3.gz > /data/jarvis.sqlite3` (into the data volume),
start it, sign in with your password, set up 2FA again if you use it, and reconnect Google if asked.

### Sharing to Jarvis and shortcuts (Android)

Once installed as an app, Jarvis appears in Android's share menu: share a WhatsApp message, a link or text and
choose Add to calendar, Remember, Track parcel (for tracking links/numbers) or Ask about it. (Images aren't
supported yet.) Long-press the app icon for shortcuts: What's on today, What's on tomorrow and Plan. Reinstalling
the app may be needed once for Android to pick up the share menu entry.
- The brief covers:
  - the weather;
  - today's events, and tomorrow's first one;
  - reminders and home actions due today;
  - anything waiting for your OK;
  - email that looks like it needs a reply;
  - birthdays this week (from Google Contacts);
  - any Home Assistant sensors you list in `BRIEF_HA_ENTITIES`.
- Scripts build it all. When the PC is on, the model only adds a short greeting.
- To see it at any time, ask "morning brief" in chat or press **Today's brief** on the **Plan** tab. It works with **▶ Speak**.

### Reminders and home actions

- **Reminders** are saved straight away, as soon as Jarvis can read a time from your message:
  - "remind me to call the garage tomorrow at 9am"
  - "remind me to put the bins out every Wednesday at 8pm"
- **Home actions** always ask you to **Confirm** first:
  - "turn off the kitchen lights at 11pm"
  - "every weekday at 7am turn on the coffee machine"
  - "set the living room thermostat to 21 degrees"
- A home action with no time runs as soon as you confirm it. A timed one runs at the set time, and repeats if you asked it to.
- Jarvis won't unlock doors or disarm alarms.
- If Jarvis was offline for more than 30 minutes past the time, the action is skipped and you're told.
- Everything open is listed on the **Plan** tab, where you can cancel it. It's also in `Jarvis/Reminders.md` in Obsidian Tasks format; ticking an item there cancels it.
- Status questions ("is the garage door open?") read Home Assistant live.

### People from Google Contacts

- Every 6 hours, each contact gets a `People/` note with a **Contact** section: phones, emails, birthday, work, address, and relations linked to their own notes.
- If a note already exists for one of their email addresses, Jarvis adds to that note instead of making a duplicate.
- Your own text and any frontmatter you've set are never overwritten.

### Internet lookups

- For general questions ("how long to hard-boil an egg?", "who won the match last night?", "opening times of Kirkgate Market"), Jarvis:
  1. searches the web;
  2. reads the top few pages;
  3. keeps only the paragraphs that match your question;
  4. answers with numbered citations — tap **[1]** to open the source.
- Saying "search the web for …", "look up …" or "google …" always searches.
- **Search engine:** SearXNG, in the `searxng` container. It's only reachable from inside Docker, not through SWAG.
- **One-time SearXNG setup:** before first start, give it a secret key:

  ```sh
  cd /opt/jarvis
  sed -i "s|CHANGE_ME|$(openssl rand -hex 32)|" searxng/settings.yml
  docker compose up -d searxng
  ```

- **Brave instead:** to use the Brave Search API, set `WEB_SEARCH_PROVIDER=brave` and `BRAVE_API_KEY`.
- **What leaves your network:** only the question itself goes to the search engines. Notes, email and other personal data are never sent.
- **Page fetching is locked down:** Jarvis won't fetch pages on your home network (router, Home Assistant, PC), and results are cached for an hour.
- **Turning it off:** set `WEB_SEARCH_PROVIDER=off`.

### Diagnostics (Logs tab)

- **Traces.** Everything Jarvis does is logged in the **Logs** tab and grouped into *runs*: each chat message, each background job that did something, and each button action.
- **Activity view.** Lists runs newest first: ✓ ok, **!** had a warning, **✗** failed. Tap one to see each step with timings and details. Tick **Problems only** to see just the runs that went wrong.
- **All entries view.** A flat, searchable list you can filter by level and source (`gmail`, `model`, `obsidian`, `home`, `events`, `notify` and so on). Tap an entry to see its data or error traceback.
- **What gets recorded:**
  - Every outgoing request (Ollama, Obsidian, Google, Home Assistant, ntfy, Kokoro, weather), with status and duration.
  - The router's decision and the model's raw answer.
  - Which notes were picked for an answer, and why.
  - Why each email was kept or skipped as bulk.
  - Why each possible event was proposed or skipped.
  - Why each notification was held.
  - Which Home Assistant entities matched a command, with their scores.
  - Model timings, including model load time on the PC.
  - Errors in the browser.
- **In chat:** **🔍 Details** under any reply shows that run's steps. On **Status**, each job has a **Logs** button for its last run.
- **Verbose** records full detail for an hour: prompts, model output, and request and response bodies. It includes email and note contents, so switch it off when you're done. Secrets (tokens, passwords, API keys) are always removed.
- **Download** saves the last 24 hours, or a single run, as JSON, to keep or share.
- Logs are stored in the Jarvis database and kept for `JARVIS_LOG_RETENTION_DAYS` (7 by default).

### Voice replies

- Tap **Voice** in the header to have replies read aloud, or tap **▶ Speak** on any single reply.
- With `JARVIS_TTS_PROVIDER=kokoro`, the `kokoro` container on the server speaks with Kokoro-82M on the CPU. The first request after it starts takes a little longer while it loads.
- To change the voice, set `JARVIS_TTS_VOICE` (for example `bm_george`, `bm_lewis`, `bf_emma`, `bf_isabella`).
- `elevenlabs` uses your ElevenLabs account instead (reply text is sent to ElevenLabs). `browser` uses the phone's or PC's built-in voice and needs no setup.
- If the server's voice fails, Jarvis falls back to the built-in voice automatically.

The **Vault** tab lists every note Jarvis has changed, with a **Revert** button for each change.

## 7. Phone

- **Jarvis:** open https://jarvis.keeeys.uk in Chrome (Android) or Safari (iOS) and use *Add to Home screen*.
- **Notifications:** install the ntfy app. Add the server `https://ntfy.keeeys.uk`, sign in as `chris`, and subscribe to the topic `jarvis`.

## 8. Updating

On the PC:

```powershell
.\scripts\build-push.ps1
```

On the server:

```sh
cd /opt/jarvis
docker compose pull
docker compose up -d
```

To pin a specific build, set `JARVIS_TAG=<date tag>` in `.env`.

### One command from WSL

`scripts/deploy.sh` builds and pushes the image, connects to the server over ssh to pull and restart Jarvis, then waits until `https://jarvis.keeeys.uk/healthz` reports the new version.

One-time setup, in WSL:

```sh
cd /mnt/d/Dev/jarvis
cp .env.deploy.example .env.deploy        # then set DEPLOY_SERVER=<you>@192.168.68.203
ssh-keygen -t ed25519                     # skip if you already have ~/.ssh/id_ed25519
ssh-copy-id <you>@192.168.68.203          # the script needs ssh without a password prompt
```

After that, one command updates everything:

```sh
bash scripts/deploy.sh
```

If `docker` needs `sudo` on the server, allow it without a password for that user, and set `DEPLOY_DOCKER="sudo -n docker"`.

### Letting Claude deploy for you

Claude can write files in `D:\Dev\jarvis`, but it can't run commands in WSL or reach the server. To close that gap, leave a watcher running in a WSL terminal:

```sh
bash scripts/deploy-watch.sh
```

When Claude finishes a change, it creates `releases/DEPLOY_REQUEST`. The watcher then:

- runs `scripts/deploy.sh`;
- writes the outcome to `releases/DEPLOY_RESULT`;
- writes the full output to `releases/deploy-<time>.log`.

Claude reads those files back and tells you whether the deploy worked. The request file is only a signal: its contents are never run. Stop the watcher with Ctrl+C whenever you like.

## 9. Backups

- Back up the `jarvis-data` volume. It holds the SQLite database (ingested email and events, the vault change log, sessions) and the Google token.
- The vault is part of your Nextcloud files, so your Nextcloud backup covers it. Nextcloud also keeps earlier versions of every note (*Versions* in the web UI).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Anything behaves oddly | Open **Logs** and tick **Problems only**, or tap **🔍 Details** under the reply. For more detail, turn on **Verbose**, repeat the action, and check the run again. |
| Model card red | `curl http://<pc>:11434/api/tags` from the server. Check `OLLAMA_HOST`, the firewall rule, and that the model is pulled. |
| Vault card: "not found" | `VAULT_HOST_PATH` doesn't point at the folder. Check it with `docker exec jarvis ls /vault`. |
| Vault card: "not readable" | `VAULT_GID` must match the group that owns Nextcloud's files (`stat -c '%g' <VAULT_HOST_PATH>`). |
| Vault card: "rejected the app password" / HTTP 400 | Check `NEXTCLOUD_USER` and `NEXTCLOUD_APP_PASSWORD`. HTTP 400 usually means `NEXTCLOUD_URL` isn't in Nextcloud's `trusted_domains`. |
| Vault warning "saved via Nextcloud but it isn't visible" | `VAULT_HOST_PATH` and `NEXTCLOUD_VAULT_DIR` point at different folders. |
| Google: redirect_uri_mismatch | The redirect URI in Google Cloud must be exactly `https://jarvis.keeeys.uk/auth/google/callback` and `JARVIS_PUBLIC_URL` must match. |
| Google stops working after a week | The app is still in *Testing*. Publish it and connect Google again. |
| Chat replies arrive all at once | Make sure `proxy_buffering off;` is in the SWAG config for Jarvis. |
| Locked out | Wait 15 minutes. To reset the password: `docker exec -it jarvis python -m jarvis.admin set-password`. |
| Events tab says Jarvis can't add events | Your Google connection predates calendar write access. Add the `calendar.events` scope to the consent screen, then press **Connect Google** again. |
| No events found in an email you expected | Check the **events** job on Status. If the model is offline, emails wait in the queue. If the daily limit was reached, they're read the next day. |
| Voice card fine but no sound on iPhone | Tap **Voice** off and on again: iOS only allows sound that starts from a tap. Also check the silent switch. |
| Home Assistant card red | `curl -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/` from the server should return `API running.` |
| "I couldn't find anything called …" | Use the name shown in Home Assistant (for example "hall light"). Entities hidden or disabled in HA are not visible. |
| No People notes from Contacts | The **People API** must be enabled, and Google must be connected with the `contacts.readonly` scope. Then run the **contacts** job on Status. |
| Internet search card red | `docker compose logs searxng`. Check that `searxng/settings.yml` has its secret key set and lists `json` under `search.formats`. |
| Web answers say the results didn't cover it | Open **🔍 Details**. It shows the search query, the results, and which pages were read or skipped. Some sites block automated reading, so rephrase the question or say "look up …" with more specific words. |
| Kokoro errors | `docker compose logs kokoro`. The first start downloads the model. |
| Lost authenticator | `docker exec -it jarvis python -m jarvis.admin totp-disable`, then run `totp-setup` again. |

---

## Appendix: the old setup (Obsidian on the PC)

To keep the vault on the PC instead, set `VAULT_BACKEND=obsidian` and the `OBSIDIAN_*` settings. Then, on the PC:

- install the **Local REST API** and **Dataview** plugins;
- set the plugin's binding host to `0.0.0.0`;
- allow port 27124 from the server in the Windows firewall.

Vault access then depends on the PC being on with Obsidian open.
