"""Google Calendar REST API access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .oauth import GoogleOAuth

BASE = "https://www.googleapis.com/calendar/v3"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_event(event: dict, calendar_id: str) -> dict:
    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    all_day = "date" in start and "dateTime" not in start
    attendees = [
        {"email": (a.get("email") or "").casefold(), "name": a.get("displayName") or "",
         "self": bool(a.get("self")), "response": a.get("responseStatus", "")}
        for a in event.get("attendees", []) or []
        if not a.get("resource")
    ]
    organizer = event.get("organizer", {}) or {}
    if organizer.get("email") and not organizer.get("self") and \
            organizer["email"].casefold() not in {a["email"] for a in attendees}:
        attendees.append({"email": organizer["email"].casefold(), "name": organizer.get("displayName", ""),
                          "self": False, "response": "organizer"})
    return {
        "event_id": event["id"],
        "calendar_id": calendar_id,
        "summary": (event.get("summary") or "").strip(),
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "all_day": all_day,
        "location": (event.get("location") or "").strip(),
        "description": event.get("description") or "",
        "attendees": attendees,
        "status": event.get("status", "confirmed"),
        "updated": event.get("updated", ""),
        "html_link": event.get("htmlLink", ""),
        "ical_uid": event.get("iCalUID", ""),
    }


class Calendar:
    def __init__(self, oauth: GoogleOAuth) -> None:
        self.oauth = oauth

    async def events(self, calendar_id: str, time_min: datetime, time_max: datetime,
                     query: str | None = None, limit: int = 1000) -> list[dict]:
        items: list[dict] = []
        page = None
        while len(items) < limit:
            params = {
                "timeMin": _iso(time_min),
                "timeMax": _iso(time_max),
                "singleEvents": "true",
                "orderBy": "startTime",
                "showDeleted": "true",
                "maxResults": 250,
            }
            if query:
                params["q"] = query
                params.pop("showDeleted")
            if page:
                params["pageToken"] = page
            data = await self.oauth.get(f"{BASE}/calendars/{quote(calendar_id, safe='')}/events", params)
            items += [normalize_event(e, calendar_id) for e in data.get("items", []) or [] if e.get("id")]
            page = data.get("nextPageToken")
            if not page:
                break
        return items

    async def calendars(self) -> list[dict]:
        """Every calendar Chris can see, hidden ones included: [{id, name, primary, writable, hidden}]."""
        data = await self.oauth.get(f"{BASE}/users/me/calendarList", {"maxResults": 250, "showHidden": "true"})
        return [{"id": c.get("id", ""), "name": c.get("summaryOverride") or c.get("summary", ""),
                 "primary": bool(c.get("primary")), "writable": c.get("accessRole") in ("owner", "writer"),
                 "hidden": bool(c.get("hidden"))}
                for c in data.get("items", []) or [] if c.get("id")]

    async def get_event(self, calendar_id: str, event_id: str) -> dict:
        return await self.oauth.get(f"{BASE}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}")

    async def patch(self, calendar_id: str, event_id: str, body: dict) -> dict:
        return await self.oauth.request(
            "PATCH", f"{BASE}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}", json_body=body)

    async def insert(self, calendar_id: str, body: dict) -> dict:
        return await self.oauth.post(f"{BASE}/calendars/{quote(calendar_id, safe='')}/events", body)

    async def upcoming(self, calendar_id: str, days: int = 14, query: str | None = None) -> list[dict]:
        now = datetime.now(timezone.utc)
        return await self.events(calendar_id, now - timedelta(days=1), now + timedelta(days=days), query)
