"""Google People API (contacts, read-only)."""

from __future__ import annotations

from .oauth import GoogleOAuth

BASE = "https://people.googleapis.com/v1"
FIELDS = ("names,nicknames,emailAddresses,phoneNumbers,birthdays,relations,organizations,addresses,"
          "biographies,memberships")


def normalize_contact(person: dict) -> dict | None:
    names = person.get("names") or []
    name = (names[0].get("displayName") or "").strip() if names else ""
    if not name:
        return None
    birthday = ""
    for entry in person.get("birthdays") or []:
        date = entry.get("date") or {}
        if date.get("month") and date.get("day"):
            birthday = (f"{date['year']:04d}-" if date.get("year") else "--") + f"{date['month']:02d}-{date['day']:02d}"
            break
    orgs = person.get("organizations") or []
    org = ""
    if orgs:
        org = ", ".join(x for x in (orgs[0].get("title", ""), orgs[0].get("name", "")) if x)
    return {
        "resource_name": person["resourceName"],
        "etag": person.get("etag", ""),
        "name": name,
        "nicknames": [n.get("value", "") for n in person.get("nicknames") or [] if n.get("value")],
        "emails": [e["value"].strip().casefold() for e in person.get("emailAddresses") or [] if e.get("value")],
        "phones": [p.get("value", "").strip() for p in person.get("phoneNumbers") or [] if p.get("value")],
        "birthday": birthday,
        "relations": [{"person": r.get("person", ""), "type": r.get("formattedType") or r.get("type", "")}
                      for r in person.get("relations") or [] if r.get("person")],
        "organization": org,
        "addresses": [a.get("formattedValue", "").replace("\n", ", ") for a in person.get("addresses") or []
                      if a.get("formattedValue")],
        "notes": "\n".join(b.get("value", "") for b in person.get("biographies") or [] if b.get("value")).strip(),
    }


class Contacts:
    def __init__(self, oauth: GoogleOAuth) -> None:
        self.oauth = oauth

    async def all(self, limit: int = 5000) -> list[dict]:
        people: list[dict] = []
        page = None
        while len(people) < limit:
            params = {"personFields": FIELDS, "pageSize": 1000, "sortOrder": "LAST_MODIFIED_DESCENDING"}
            if page:
                params["pageToken"] = page
            data = await self.oauth.get(f"{BASE}/people/me/connections", params)
            people += [p for p in (normalize_contact(x) for x in data.get("connections", []) or []) if p]
            page = data.get("nextPageToken")
            if not page:
                break
        return people
