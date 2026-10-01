# Natural-language intelligence

Jarvis can infer a read-only data source from ordinary language. The current planner recognizes five routes:

- `chat`: answer with the local Ollama conversation.
- `obsidian`: search the configured Obsidian vault.
- `gmail`: search Gmail threads.
- `calendar`: search calendar events.
- `drive`: search Drive files.

For an integration route, Jarvis calls one approved read-only MCP tool, then gives the returned data and the original request to local Ollama for a concise answer. The answer should identify note names, senders, event titles, or file names when the source provides them. Retrieved personal data remains in the local Jarvis process and local Ollama request; it is not automatically sent to Codex or another cloud reasoning service.

The planner validates its output against the five routes and has a conservative keyword fallback if the small model does not return valid JSON. Requests that look like writes—sending, editing, deleting, scheduling, moving, renaming, or sharing—are not executed by this slice. The chat interface is natural-language-first; only session controls such as `:status`, `:voice`, and `:quit` remain command-based.

## Examples

```text
Find the notes about the desk buddy hardware
What meetings do I have tomorrow?
Find recent emails from Alice about the project
Search Drive for the latest budget
```

Write actions and automatic Codex escalation remain future slices.
