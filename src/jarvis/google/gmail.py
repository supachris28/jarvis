"""Gmail REST API access and pure parsing helpers (no LLM involved)."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr

from .oauth import GoogleOAuth

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
BULK_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
NOREPLY = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|notifications?@|bounce)", re.I)


@dataclass
class ParsedMessage:
    message_id: str
    thread_id: str
    ts: float
    from_addr: str
    from_name: str
    to: list[tuple[str, str]]
    subject: str
    labels: list[str]
    snippet: str
    body: str
    attachments: list[str] = field(default_factory=list)
    list_unsubscribe: bool = False
    html: str = ""                                                   # raw HTML (for JSON-LD), not stored
    ics_inline: list[str] = field(default_factory=list)              # text/calendar parts with inline data
    ics_attachment_ids: list[str] = field(default_factory=list)      # .ics attachments to fetch
    forwarded_from: str = ""                                         # a forwarded email: who first sent it
    forwarded_name: str = ""

    @property
    def sender_name(self) -> str:
        """Who the email is really from — the original sender of a forwarded email."""
        return self.forwarded_name or (self.forwarded_from.split("@")[0] if self.forwarded_from else self.from_name)

    @property
    def promotional(self) -> bool:
        return bool(BULK_CATEGORIES.intersection(self.labels)) or "SPAM" in self.labels

    @property
    def outgoing(self) -> bool:
        return "SENT" in self.labels

    @property
    def bulk(self) -> bool:
        if self.outgoing:
            return False
        if BULK_CATEGORIES.intersection(self.labels) or "SPAM" in self.labels:
            return True
        return self.list_unsubscribe or bool(NOREPLY.search(self.from_addr))

    @property
    def bulk_reason(self) -> str:
        if self.outgoing:
            return ""
        categories = BULK_CATEGORIES.intersection(self.labels)
        if categories:
            return "Gmail category " + ", ".join(sorted(categories))
        if "SPAM" in self.labels:
            return "spam"
        if self.list_unsubscribe:
            return "has List-Unsubscribe header (mailing list)"
        if NOREPLY.search(self.from_addr):
            return "automated sender address"
        return ""

    @property
    def personal(self) -> bool:
        """Likely from a real person you know: not bulk, not an automated category."""
        if self.bulk:
            return False
        return self.outgoing or "CATEGORY_UPDATES" not in self.labels


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", markup)
    markup = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(markup)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


QUOTE_START = re.compile(r"^(On .{5,200}wrote:|-{2,} ?Original Message ?-{2,}|From: .+|Sent from my \w+)", re.I)
# the start of a forwarded email (Gmail, Apple Mail, Outlook)
FORWARD_MARK = re.compile(r"^[ \t>]*(?:-{3,}\s*Forwarded message\s*-{3,}|Begin forwarded message:)[ \t]*$", re.I | re.M)
# also used under replies, so only counted when the subject says it's a forward
WEAK_FORWARD_MARK = re.compile(r"^[ \t>]*(?:-{3,}\s*Original Message\s*-{3,}|_{10,})[ \t]*$", re.I | re.M)
FORWARD_HEADER = re.compile(r"^[ \t>*]*(From|Date|Sent|Subject|To|Cc|Reply-To)\s*:\**\s*(.*)$", re.I)
FORWARD_SUBJECT = re.compile(r"^\s*(?:fwd?|fw)\s*:", re.I)


def split_forward(text: str, subject: str = "") -> tuple[str, dict, str] | None:
    """(your note, the forwarded email's headers, the forwarded email's text) — or None if nothing is forwarded."""
    text = text.replace("\r\n", "\n")
    match = FORWARD_MARK.search(text) or (WEAK_FORWARD_MARK.search(text) if FORWARD_SUBJECT.match(subject or "") else None)
    start = match.end() if match else None
    if start is None and FORWARD_SUBJECT.match(subject or ""):
        # Outlook and some phones: no marker line, just a From:/Sent:/To:/Subject: block
        block = re.search(r"^[ \t>*]*From\s*:.+\n(?:[ \t>*]*(?:Sent|Date|To|Cc|Subject)\s*:.*\n){2,5}", text, re.I | re.M)
        if block:
            match, start = block, block.start()
    if start is None:
        return None
    note = text[:match.start()]
    headers: dict[str, str] = {}
    lines = text[start:].split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    while index < len(lines):
        header = FORWARD_HEADER.match(lines[index])
        if not header:
            break
        headers.setdefault(header.group(1).casefold(), header.group(2).strip())
        index += 1
    if not headers:
        return None
    body = "\n".join(re.sub(r"^>[ ]?", "", line) for line in lines[index:])
    return note, headers, body


def clean_body(text: str, limit: int, subject: str = "", _depth: int = 0) -> str:
    forward = split_forward(text, subject) if _depth < 3 else None
    if forward:
        note, headers, inner = forward
        mine = clean_body(note, limit, _depth=_depth + 1)
        sent = headers.get("date") or headers.get("sent") or ""
        heading = (f"--- Forwarded email from {headers.get('from', 'someone')}"
                   + (f", sent {sent}" if sent else "") + (f": {headers['subject']}" if headers.get("subject") else "")
                   + " ---")
        theirs = clean_body(inner, limit, headers.get("subject", ""), _depth + 1)
        body = (mine + "\n\n" if mine else "") + heading + "\n" + theirs
        return body if len(body) <= limit else body[:limit].rstrip() + "\n\n…(truncated)"
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if QUOTE_START.match(stripped) and lines:
            break
        if stripped.startswith(">"):
            continue
        if stripped == "--":  # signature delimiter
            break
        lines.append(line.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return body if len(body) <= limit else body[:limit].rstrip() + "\n\n…(truncated)"


def _walk(part: dict, plain: list[str], rich: list[str], attachments: list[str], ics: list[str],
          ics_ids: list[str]) -> None:
    mime = (part.get("mimeType") or "").casefold()
    filename = part.get("filename") or ""
    body = part.get("body", {}) or {}
    data = body.get("data")
    if mime in {"text/calendar", "application/ics"} or filename.casefold().endswith(".ics"):
        if data:
            ics.append(_decode(data))
        elif body.get("attachmentId"):
            ics_ids.append(body["attachmentId"])
        if filename:
            attachments.append(filename)
    elif filename:
        attachments.append(filename)
    elif mime == "text/plain" and data:
        plain.append(_decode(data))
    elif mime == "text/html" and data:
        rich.append(_decode(data))
    for child in part.get("parts", []) or []:
        _walk(child, plain, rich, attachments, ics, ics_ids)


def parse_message(message: dict, body_limit: int = 8000) -> ParsedMessage:
    payload = message.get("payload", {}) or {}
    headers = {h.get("name", "").casefold(): h.get("value", "") for h in payload.get("headers", []) or []}
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []
    ics: list[str] = []
    ics_ids: list[str] = []
    _walk(payload, plain, rich, attachments, ics, ics_ids)
    text = "\n".join(plain).strip() or html_to_text("\n".join(rich))
    from_name, from_addr = parseaddr(headers.get("from", ""))
    recipients = getaddresses([headers.get("to", ""), headers.get("cc", "")])
    return ParsedMessage(
        message_id=message["id"],
        thread_id=message.get("threadId", message["id"]),
        ts=int(message.get("internalDate", "0")) / 1000,
        from_addr=from_addr.casefold(),
        from_name=from_name.strip().strip('"'),
        to=[(addr.casefold(), name.strip().strip('"')) for name, addr in recipients if addr],
        subject=headers.get("subject", "").strip(),
        labels=list(message.get("labelIds", []) or []),
        snippet=html.unescape(message.get("snippet", "")),
        body=clean_body(text, body_limit, headers.get("subject", "")),
        attachments=attachments,
        list_unsubscribe="list-unsubscribe" in headers,
        html="\n".join(rich)[:300_000],
        ics_inline=ics,
        ics_attachment_ids=ics_ids,
        **forwarded_sender(text, headers.get("subject", "")),
    )


def forwarded_sender(text: str, subject: str) -> dict:
    forward = split_forward(text, subject)
    if not forward:
        return {}
    name, address = parseaddr(forward[1].get("from", ""))
    return {"forwarded_from": address.casefold() or forward[1].get("from", "")[:100],
            "forwarded_name": name.strip().strip('"')}


class Gmail:
    def __init__(self, oauth: GoogleOAuth) -> None:
        self.oauth = oauth

    async def profile(self) -> dict:
        return await self.oauth.get(f"{BASE}/profile")

    async def message(self, message_id: str) -> dict:
        return await self.oauth.get(f"{BASE}/messages/{message_id}", {"format": "full"})

    async def attachment(self, message_id: str, attachment_id: str) -> str:
        data = await self.oauth.get(f"{BASE}/messages/{message_id}/attachments/{attachment_id}")
        return _decode(data.get("data", ""))

    async def list_message_ids(self, query: str, limit: int = 500) -> list[str]:
        ids: list[str] = []
        page = None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(ids))}
            if page:
                params["pageToken"] = page
            data = await self.oauth.get(f"{BASE}/messages", params)
            ids += [m["id"] for m in data.get("messages", []) or []]
            page = data.get("nextPageToken")
            if not page:
                break
        return ids

    async def history(self, start_history_id: str) -> tuple[list[str], str]:
        """Message IDs added since start_history_id, and the newest history id."""
        ids: list[str] = []
        page = None
        latest = start_history_id
        while True:
            params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "maxResults": 500}
            if page:
                params["pageToken"] = page
            data = await self.oauth.get(f"{BASE}/history", params)
            for record in data.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    message_id = added.get("message", {}).get("id")
                    if message_id and message_id not in ids:
                        ids.append(message_id)
            latest = str(data.get("historyId", latest))
            page = data.get("nextPageToken")
            if not page:
                return ids, latest

    async def thread_messages(self, thread_id: str) -> list[dict]:
        """Every message in a thread, in full (for re-reading old emails)."""
        data = await self.oauth.get(f"{BASE}/threads/{thread_id}", {"format": "full"})
        return data.get("messages", []) or []

    async def search_threads(self, query: str, limit: int = 15) -> list[dict]:
        listed = await self.oauth.get(f"{BASE}/threads", {"q": query, "maxResults": limit})
        results = []
        for thread in listed.get("threads", []) or []:
            detail = await self.oauth.get(
                f"{BASE}/threads/{thread['id']}",
                [("format", "metadata"), ("metadataHeaders", "Subject"), ("metadataHeaders", "From"),
                 ("metadataHeaders", "Date")],
            )
            messages = detail.get("messages", []) or [{}]
            headers = {h.get("name", "").casefold(): h.get("value", "")
                       for h in messages[-1].get("payload", {}).get("headers", [])}
            results.append({
                "id": thread["id"],
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "date": headers.get("date", ""),
                "messages": len(messages),
                "snippet": html.unescape(detail.get("snippet", messages[-1].get("snippet", ""))),
            })
        return results


def thread_url(thread_id: str, account: str = "") -> str:
    """Link that opens a thread in Gmail on the web, on the right Google account.

    /mail/u/<address>/ picks the account by address. (/mail/u/0/ means "whichever account you signed into first", and
    the ?authuser= form gets redirected in a way that drops the #thread part, landing on the inbox.)"""
    if not thread_id:
        return ""
    from urllib.parse import quote
    return f"https://mail.google.com/mail/u/{quote(account, safe='@') if account else '0'}/#all/{thread_id}"


def app_email_url(thread_id: str, public_url: str = "") -> str:
    """Link to the email inside Jarvis — works on every device, including the Android app, where Gmail web links
    can't open a particular email."""
    if not thread_id:
        return ""
    return f"{public_url.rstrip('/')}/#email?thread={thread_id}"
