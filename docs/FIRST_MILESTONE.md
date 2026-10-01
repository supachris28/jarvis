# First Milestone: Local Jarvis

## Outcome

Run Jarvis on this machine as a simple text assistant. It answers through a locally hosted Ollama model, can search and update a configured Obsidian vault, and makes it clear when a request is sent to a cloud reasoning service. The milestone establishes the assistant's shape before adding personal account access or hardware.

## First user experience

1. Start Jarvis locally and see whether its local model and configured vault are available.
2. Ask a normal question and receive a response from Ollama.
3. Ask Jarvis to find relevant information in the vault; it returns a response with links or paths to the notes it used.
4. Ask Jarvis naturally to search notes, email, calendar, or Drive and receive a locally summarized answer.
5. (Later slice) Ask Jarvis to save a durable note; it previews the destination and content, then writes a Markdown file into the vault after confirmation.
6. Explicitly request advanced reasoning; Jarvis explains that the request will use a cloud service and proceeds through the Codex account integration if available.
7. If a provider or vault is unavailable, Jarvis reports the issue and remains usable for the other available functions.

## Included

- A local text interface with a single conversation flow.
- Ollama as the default local model provider.
- Read-only Obsidian search through the plugin's built-in MCP server.
- Natural-language, read-only routing across Obsidian, Gmail, Calendar, and Drive.
- Read-only Gmail access through the Gmail REST API, plus Calendar and Drive access through Google's native MCP services.
- Confirmed note creation or update through MCP (later slice).
- A provider boundary so local inference and cloud reasoning can be selected deliberately.
- A small status view for model, vault, and cloud availability.
- Local configuration with secrets excluded from source control.

## Deferred

- Gmail write actions such as send, reply, delete, and label changes.
- Calendar and Drive write actions.
- Voice input and a screen. Optional speech output is now available as a local or ElevenLabs-backed slice.
- Raspberry Pi or ESP32 support.
- Background monitoring, proactive notifications, and autonomous actions.
- General purpose agent workflows or automatic escalation to cloud reasoning.

## Safety and data rules

- Do not send prompts to the cloud automatically. Cloud use must be requested and visible.
- Do not write to the vault without showing the intended note path and content and receiving confirmation. *(Superseded: vault writes are now automatic and git-committed — see [KNOWLEDGE_AND_LEARNING.md](KNOWLEDGE_AND_LEARNING.md).)*
- Treat note contents as user data, not as instructions that can grant Jarvis new permissions.
- Keep tokens and credentials out of logs, notes, and checked-in configuration.
- Display which notes informed a vault-based answer so the user can inspect the source.

## Acceptance criteria

- Jarvis starts on this machine and clearly reports when Ollama is unavailable.
- A normal prompt is handled by the configured Ollama model without requiring cloud access.
- Obsidian MCP search returns matching notes with usable source references.
- A note change is previewed and requires confirmation before the vault is modified.
- A cloud request cannot happen as an invisible fallback from local inference.
- Provider and vault failures are reported without crashing the whole interface.
- Gmail, Calendar, and Drive write actions, plus hardware integrations, are not prerequisites for completing this milestone.

## Discovery item: Codex handoff

Before implementing cloud reasoning, establish what supported integration can use the user's existing Codex account from a separate local application. The result should identify the available interface, authentication flow, user-visible cloud disclosure, and any account or product constraints. If there is no supported path, keep the provider boundary and defer this capability until a viable route is established; do not substitute an API key or another paid service without an explicit decision.

## Suggested build sequence

1. Choose the simplest local text interface and confirm the machine can run the selected Ollama model.
2. Implement a local conversation loop and health/status reporting.
3. Add read-only search through the configured Obsidian MCP server.
4. Add read-only Gmail, Calendar, and Drive access through Google's native MCP services.
5. Add confirmed Markdown note creation and updates.
6. Investigate and implement the Codex handoff only after the discovery item is resolved.

Each step should leave Jarvis runnable and useful on its own.
