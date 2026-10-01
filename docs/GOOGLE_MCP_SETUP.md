# Google integration setup

Jarvis uses the normal Gmail REST API for Gmail and Google’s native remote MCP services for Calendar and Drive. Gmail’s MCP endpoint is not required.

## Google Cloud setup

1. In the Google Cloud project you already created, enable the Gmail API, Google Calendar API, and Google Drive API.
2. Configure Google Auth Platform and add your account as a test user if the app is External and still in testing.
3. Create or use an OAuth client ID and secret. For local Jarvis, the simplest choice is a Desktop application client.
4. If you use a Web application client, add this exact authorized redirect URI:

   `http://127.0.0.1:8765/oauth2callback`

5. Ensure the OAuth consent screen includes these read-only scopes:

   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
   - `https://www.googleapis.com/auth/calendar.events.freebusy`
   - `https://www.googleapis.com/auth/calendar.events.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`

The scopes keep this integration read-only. Gmail search uses the Gmail API’s normal search syntax, such as `from:someone@example.com newer_than:7d`. Drafts, sends, event changes, and Drive writes are deliberately deferred.

## Local configuration

Your `.env` should contain:

```text
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_REDIRECT_URI=http://127.0.0.1:8765/oauth2callback
GOOGLE_TOKEN_PATH=.jarvis/google-token.json
```

`GOOGLE_TOKEN_PATH` stores the access and refresh token locally with restrictive permissions. `.jarvis/` and `.env` are ignored by Git.

## Authenticate

From the Jarvis directory, run:

```sh
python3 jarvis.py auth
```

Jarvis prints an authorization URL and opens it in the default browser. Approve the requested read-only scopes. Google redirects to the local callback and Jarvis stores the token. The same token is reused for the Gmail API, Calendar MCP, and Drive MCP clients and refreshed when needed.

If the token was created before changing scopes, delete `.jarvis/google-token.json` and run authentication again.

## Use the connectors

Start Jarvis:

```sh
python3 jarvis.py
```

Ask naturally, for example:

```text
Find recent emails from Alice about the project
What meetings do I have tomorrow?
Search Drive for the latest budget
Find the notes about the desk buddy hardware
```

Gmail results contain thread metadata and snippets only; message bodies are not fetched in this slice. Retrieved results can be summarized by the local Ollama model.

## Troubleshooting

- `configured, not authenticated`: run `python3 jarvis.py auth`.
- Redirect URI errors: make sure the OAuth client type and the URI in Google Cloud match the setup above exactly.
- Scope errors: update the consent screen scopes, delete `.jarvis/google-token.json`, and authenticate again.
- Gmail API errors: check that the Gmail API is enabled in the same project as the OAuth client.
- Calendar or Drive MCP errors: check that the corresponding Google API and native MCP service are enabled and available to the account.
