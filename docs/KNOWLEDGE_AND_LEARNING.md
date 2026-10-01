# Jarvis Knowledge and Learning

This document describes how Jarvis builds up knowledge about you, your life, and the people around you in Obsidian. It also covers how Jarvis keeps LLM use low by using scripts for repeatable work, and how it improves itself, including learning to use new MCP tools and APIs. Where Jarvis runs is described in [ARCHITECTURE.md](ARCHITECTURE.md).

## Principles

1. **Store aggressively, organise cheaply.** Jarvis captures everything that isn't bulk mail or noise. Scripts do the capturing and filing. The LLM only adds understanding on top.
2. **Scripts first, LLM second.** Any task that happens more than a few times should end up as a script. Jarvis keeps count of how often it has to use the LLM for a given kind of task and turns frequent ones into code.
3. **The vault is the memory.** Anything Jarvis knows is stored as plain Markdown that you can read, edit, and delete. The SQLite database only holds an index that can be rebuilt from the vault.
4. **Every change can be undone.** Jarvis can write anywhere in the vault, and every write is logged with the text before and after, so it can be reverted.
5. **Untrusted content never steers Jarvis.** Email, web pages, and files are data. They are handled by scripts or by LLM calls that have no tools.

## Vault structure

Jarvis may write anywhere in the vault, but its scripts follow a fixed layout so the same kind of information always ends up in the same place:

```text
People/            one note per person      (type: person)
Organisations/     companies, schools, clubs (type: org)
Places/            homes, venues, towns      (type: place)
Journal/YYYY/      daily notes YYYY-MM-DD.md with a "## Jarvis" log section
Sources/
  Email/YYYY/MM/   one note per stored thread
  Calendar/YYYY/   one note per event
  Chat/YYYY/MM/    facts captured from conversations with Jarvis
Inbox/             quick captures not yet filed
Jarvis/
  Me.md            facts about you
  Preferences.md   how Jarvis should behave; corrections you've made
  Rules.md         notification and filing rules (editable by you and Jarvis)
  Tools/           one note per connector or MCP server: what it does, usage lessons
  Skills/          one readable note per learned skill (the code lives in the skills repo)
```

### Note conventions

Each note has frontmatter that scripts use to find and match it. Anything Jarvis maintains automatically sits between markers, so a script can rewrite that block every time without touching text you wrote:

```markdown
---
type: person
aliases: [Sam, Samuel Jones]
emails: [sam@example.com]
phones: ["+44 7700 900123"]
relation: friend
birthday: 1990-04-12
immich_person: 3f2a…
updated: 2026-09-28
---
# Sam Jones

Your own notes go here, and Jarvis can add to them too.

<!-- jarvis:facts -->
- 2026-09-20 · Moving to Leeds in November · [[Sources/Email/2026/09/2026-09-20 House move (18c2…)]]
- 2026-08-02 · Birthday dinner at Dishoom · [[Sources/Calendar/2026/2026-08-02 Sam birthday]]
<!-- /jarvis:facts -->

<!-- jarvis:timeline -->
- Last email: 2026-09-20 · Last met: 2026-08-02 · Photos: 214 (Immich)
<!-- /jarvis:timeline -->
```

Every fact records its date and links to the note it came from, so an answer can always point back to its source.

### Vault safety

- Every write goes through the Obsidian REST API and is recorded in Jarvis's change log (`note_history`), along with what made the change (pipeline, chat or skill). The **Vault** tab in the web app lists every change and has a one-click revert. *(Implemented in v0.2.)*
- Before each write, Jarvis reads the note's current text and replaces only its own marked sections. Anything you edited in Obsidian in the meantime is kept.
- If the PC is off, writes wait in an outbox and are written once Obsidian is reachable again.
- Optional: the Obsidian Git plugin adds a second, independent history of the whole vault.
- Jarvis doesn't delete notes.
- Secrets never go into the vault.

## Processing tiers (keeping AI use low)

| Tier | What | When | Cost |
| --- | --- | --- | --- |
| **0: Scripts** | Collect, normalise, store, link, index, rule-based alerts | Every timer run | No LLM |
| **1: Local batch LLM** | Pull facts out of items that scripts flag as needing understanding | Queued; runs when the GPU is free | Small model, daily cap |
| **2: Interactive LLM** | Chat, answering questions, planning with tools | When you ask | Larger model |
| **3: Cloud** | Hard reasoning or code generation beyond the local model | Only when you ask for it explicitly, and shown in the UI | External |

### Tier 0: what scripts do on their own

- **Filter out noise.** Emails with the Gmail labels `CATEGORY_PROMOTIONS`, `CATEGORY_SOCIAL`, or `CATEGORY_FORUMS`, or with a `List-Unsubscribe` header, or from `noreply` senders, are **counted but not stored**. Rules in `Jarvis/Rules.md` can override this for specific senders.
- **Store email.** Other threads get a Source note with headers, participants, labels, attachment names, and the cleaned body text (quoted replies and signatures stripped, length capped).
- **Store calendar.** Every event gets a Source note with its attendees and location. The event is also linked from that day's Journal note.
- **Match people.** Email addresses, phone numbers, and names are looked up in people notes (`emails`, `phones`, `aliases`). Unknown people you actually correspond with get a new person note automatically. Contacts from the Contacts API seed the initial set.
- **Extract with rules.** Dates, times, phone numbers, addresses, amounts, tracking numbers, booking references, and bills are pulled out with regular expressions and sender-specific parsers. Many of those parsers are generated by Jarvis over time (see below).
- **Update links.** Each person's `timeline` block (last email, last met, photo count), the Journal log, and backlinks are kept up to date.
- **Index.** Jarvis keeps its own SQLite index of the vault files: FTS5 full-text search, tags (including nested ones), frontmatter fields, and links between notes. Changed files are re-indexed within a minute. This allows questions like "birthdays this month" to be answered without the LLM.
- **Decide what to notify.** Rules from `Rules.md` decide which items trigger an ntfy notification.
- **Queue for Tier 1.** Items the scripts can't fully handle are queued: an email from a known person with meaningful body text, a chat message that states a fact, or an unrecognised item from a sender that appears regularly.

### Tier 1: batch understanding

- The queue lives in SQLite with priorities. A worker processes it in batches whenever the GPU is free, and pauses while you chat. It stops once it reaches a daily call budget.
- Each call gets **no tools** and must return JSON that matches a fixed schema, for example `{facts:[{entity, kind, value, date, confidence}], people:[…], tasks:[…], importance}`. A script checks the result and writes it to the vault. Anything that fails the check is dropped and logged.
- Facts with low confidence go into `Inbox/` for you to look at, instead of into People notes.
- Each call is counted by **pattern**: sender domain plus subject shape, or event type. These counts drive the promote-to-script loop.

### Backfill

The first time Jarvis runs, it imports history with scripts (Tier 0):

- Contacts
- ±12 months of calendar events
- 12–24 months of non-bulk email

Tier 1 then works through the backlog slowly, most recent items first, within its daily budget.

## Retrieval

When Jarvis answers a question from the vault:

1. **People** whose name appears in the question are found through their `People/` notes.
2. **Tags and frontmatter** are looked up in the index (for example `#holiday` or `relation: cousin`).
3. **Keyword search** uses the FTS5 full-text index, ranked with BM25.
4. **Relationships** come from backlinks (the index's link table) and the note's own outlinks.
5. **Semantic search** (planned) will use embeddings from Ollama `nomic-embed-text`, stored on the server and updated when a note changes.
6. The LLM writes the answer only from the notes it retrieved, and cites them as links.

`Jarvis/Me.md` and `Jarvis/Preferences.md` are always included in the chat context. They should stay short: Jarvis condenses them when they grow.

## Self-improvement

Jarvis gets better in four ways, listed from cheapest to most powerful.

### 1. Memory and corrections

- When you correct Jarvis ("no, Sam is my cousin", "don't notify me about school newsletters"), it updates the relevant note or rule straight away. The correction is also logged in `Preferences.md` or `Rules.md`.
- Facts you state in chat are written to the vault immediately (Tier 0 for simple "remember that…" statements, Tier 1 for the rest).

### 2. Learning new tools

- **MCP servers.** You give Jarvis a URL, and it reads the server's tool list (`tools/list`). It uses the MCP tool annotations (`readOnlyHint`, `destructiveHint`) plus the tool names to mark each tool read or write. It then writes `Jarvis/Tools/<server>.md` describing each tool and registers the tools in the registry. You enter any credentials in the UI; they are stored in `/data/secrets`.
- **REST APIs with an OpenAPI spec.** A script turns the spec into tool definitions. `GET` operations are marked read and everything else is marked write, so writes go through the proposal step.
- **APIs without a spec.** The skill builder reads the documentation and **generates a connector** in the skills repo, which runs in the sandbox (see codegen below).
- **Usage lessons.** After tool calls, Jarvis adds what worked and what failed (argument formats, error causes, limits) to the tool's note. The planner includes that note whenever it considers that tool.

### 3. Skills (learned procedures)

When a request succeeds after several tool calls or reasoning steps, Jarvis saves it as a **skill** so the same request can later run without the LLM.

```yaml
# skills/bin-day/manifest.yaml
name: bin-day
description: Which bin goes out this week, from the council email
triggers: ["bin day", "which bin", "bins this week"]
kind: code            # or: steps (declarative tool sequence)
entry: main.py
tools: [vault.read, gmail.search]           # the scoped gateway token covers only these
egress: []                                  # hosts this skill may reach directly
schedule: "0 18 * * SUN"                    # optional: runs as a timer job
created_by: builder  # builder | user
tests: tests/
```

- **Matching.** The planner checks skill triggers and descriptions (keyword first, then embedding) before it calls the LLM. A match runs the skill directly, and the LLM is used only for anything the skill leaves open.
- **Declarative steps** cover sequences of tool calls with filled-in parameters. **Code** covers parsing, calculation, and new APIs.
- **Scheduled skills.** Asking "tell me every Sunday which bin goes out" produces a skill with a `schedule`. It becomes a timer job that makes no LLM calls.

### 4. Code generation and promote-to-script

This is the main way Jarvis reduces its own AI use over time.

```text
trigger ─► generate ─► static check ─► test in sandbox ─► register ─► monitor
   ▲                                                                  │
   └──────────────── repair (max N attempts) ◄── failures ◄───────────┘
```

- **Triggers:**
  - You ask ("learn how to…", "add the X API").
  - A Tier 1 pattern crosses a threshold. For example, if the council bin email has needed the LLM 5 times, Jarvis generates a parser for that sender and future emails from it are handled by Tier 0.
  - Repeated identical tool sequences in chat.
- **Generate.** The larger local model, or the cloud if you explicitly ask, writes the code and tests. The tests use saved **example inputs** from real items the LLM has already handled, with the LLM's output as the expected result.
- **Check.** The code must parse, pass `ruff`, import only modules on an allow-list, and use only the tools and hosts declared in its manifest.
- **Test.** The tests run in the runner sandbox. A new parser must match the LLM's earlier extractions on the saved examples before it replaces the LLM for that pattern.
- **Register.** Once a skill passes its tests it is enabled without review (per your choice). It is committed to the skills repo and gets a note in `Jarvis/Skills/` plus an entry in the Skills view.
- **Monitor.** Every run is logged. On failure, Jarvis attempts an automatic repair. After N failures the skill is disabled, that pattern goes back to Tier 1, and ntfy tells you.
- **Regression evals.** A set of test prompts in `tests/evals/` is run after planner or skill changes. Changes that make results worse are rolled back.

### Sandbox for generated code

Generated code runs without your review, so the sandbox is what makes that safe:

- Code runs only in the **`jarvis-runner`** container, never in the `jarvis` container. The runner:
  - is non-root, with a read-only filesystem and all capabilities dropped
  - has CPU, memory, and time limits
  - has no access to `/data`, secrets, or the vault files.
- **Credentials stay with the gateway.** Code calls existing connectors through the **tool gateway** with a per-run token covering only the manifest's `tools`. Vault writes also go through the gateway, so they are validated and recorded in the change log. External writes still become proposals.
- **New API keys.** When a generated connector needs a key for a new service, you enter it once in the UI. The gateway then attaches the key to requests to that host, so the code never sees it.
- **Controlled network access.** The runner has no direct internet access. All traffic goes through an egress proxy that only allows the hosts listed in the skill's `egress` field. A skill processing your email therefore can't send it anywhere else.
- **Kill switch.** The Skills view can pause all skills at once or disable a single one. Every version is in git, so rolling back is a `git revert`.

### Untrusted content

Automatic ingestion combined with code generation is the main risk in this design: a crafted email could try to make Jarvis write harmful code. These rules close that path:

- Content from email, web pages, and files only reaches (a) scripts, or (b) Tier 1 calls that have no tools and must return data in a fixed format.
- Code generation is triggered only by you in chat, or by the internal pattern counter. Never by what an item says.
- When example items are given to the code generator as test data, they are marked as untrusted. Whatever code comes out still runs inside the sandbox's tool, host, and write limits.
- Notes created from sources carry `source:` frontmatter. The planner treats text in those notes as quoted data, never as instructions.

## What changes from earlier docs

- [FIRST_MILESTONE.md](FIRST_MILESTONE.md) said vault writes must be previewed and confirmed. That is **superseded**: vault writes are now automatic and logged, and each one can be reverted. Confirmation still applies to actions outside the vault.
- The Obsidian MCP plugin is no longer needed. Jarvis reads the vault files directly.
- Suggested additions to [CAPABILITIES.md](CAPABILITIES.md):
  - 3.1 Build and maintain knowledge about me, my family and friends in Obsidian
  - 3.2 Prefer scripted processing; learn scripts for repeated AI work
  - 3.3 Learn new MCP tools and APIs
  - 3.4 Learn and run new skills
