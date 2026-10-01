"""Bible passages by script (Tier 0): "read me Philippians 1", "John 3:16", "what does Psalm 23 say?".

The reference is recognised by script and the text comes verbatim from bible-api.com (public-domain translations,
World English Bible British Edition by default), so the model never paraphrases scripture.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from . import diag

BOOKS = {
    "genesis": ["gen", "gn"], "exodus": ["exod", "exo", "ex"], "leviticus": ["lev", "lv"], "numbers": ["num", "nm"],
    "deuteronomy": ["deut", "dt"], "joshua": ["josh", "jos"], "judges": ["judg", "jdg"], "ruth": ["rth"],
    "1 samuel": ["1 sam", "1sam", "1 sa"], "2 samuel": ["2 sam", "2sam", "2 sa"], "1 kings": ["1 kgs", "1kgs"],
    "2 kings": ["2 kgs", "2kgs"], "1 chronicles": ["1 chron", "1 chr"], "2 chronicles": ["2 chron", "2 chr"],
    "ezra": ["ezr"], "nehemiah": ["neh"], "esther": ["esth", "est"], "job": ["jb"], "psalms": ["psalm", "ps", "psa"],
    "proverbs": ["prov", "prv", "pr"], "ecclesiastes": ["eccl", "ecc", "qoh"],
    "song of songs": ["song of solomon", "song", "sos"], "isaiah": ["isa", "is"], "jeremiah": ["jer"],
    "lamentations": ["lam"], "ezekiel": ["ezek", "ezk"], "daniel": ["dan", "dn"], "hosea": ["hos"], "joel": ["jl"],
    "amos": ["am"], "obadiah": ["obad", "ob"], "jonah": ["jon"], "micah": ["mic"], "nahum": ["nah"],
    "habakkuk": ["hab"], "zephaniah": ["zeph"], "haggai": ["hag"], "zechariah": ["zech"], "malachi": ["mal"],
    "matthew": ["matt", "mt"], "mark": ["mk", "mrk"], "luke": ["lk"], "john": ["jn", "jhn"], "acts": ["act"],
    "romans": ["rom"], "1 corinthians": ["1 cor", "1cor"], "2 corinthians": ["2 cor", "2cor"],
    "galatians": ["gal"], "ephesians": ["eph"], "philippians": ["phil", "php"], "colossians": ["col"],
    "1 thessalonians": ["1 thess", "1 thes"], "2 thessalonians": ["2 thess", "2 thes"], "1 timothy": ["1 tim"],
    "2 timothy": ["2 tim"], "titus": ["tit"], "philemon": ["philem", "phm"], "hebrews": ["heb"],
    "james": ["jas", "jam"], "1 peter": ["1 pet", "1pet"], "2 peter": ["2 pet", "2pet"], "1 john": ["1 jn", "1jn"],
    "2 john": ["2 jn"], "3 john": ["3 jn"], "jude": ["jud"], "revelation": ["rev", "revelations"],
}
NAMES: dict[str, str] = {}
for _book, _aliases in BOOKS.items():
    for _name in (_book, *_aliases):
        NAMES[_name] = _book
        for _word, _num in (("first", "1"), ("second", "2"), ("third", "3"), ("1st", "1"), ("2nd", "2"), ("3rd", "3"),
                            ("i", "1"), ("ii", "2"), ("iii", "3")):
            if _name.startswith(_num + " "):
                NAMES[_word + " " + _name[2:]] = _book
BOOK = "|".join(sorted((re.escape(n) for n in NAMES), key=len, reverse=True))
REFERENCE = re.compile(
    rf"(?<![\w-])(?P<book>{BOOK})\.?\s+(?:chapter\s+)?(?P<chapter>\d{{1,3}})"
    r"(?:\s*[:.v]\s*(?P<verse>\d{1,3})(?:\s*[-–]\s*(?P<to>\d{1,3}))?)?(?!\s*(?:am|pm|%|°))(?![\w:])", re.I)
# a Bible request, not a person called Mark or a date in June — needs a reading verb or to be just the reference
ASK = re.compile(r"\b(read|recite|show|quote|what does|what do|look up|verse|verses|chapter|passage|bible|scripture|"
                 r"psalm|say|says)\b", re.I)


def find_reference(prompt: str) -> str | None:
    """'Read me Philippians 1' → 'philippians 1'; None when it isn't a Bible request."""
    match = REFERENCE.search(prompt)
    if not match:
        return None
    rest = re.sub(r"\b(please|thanks|thank you|jarvis|can you|could you|me|for me)\b", " ",
                  prompt[:match.start()] + prompt[match.end():], flags=re.I).strip(" ?.!,")
    if rest and not ASK.search(prompt):
        return None
    book = NAMES[re.sub(r"\s+", " ", match.group("book").casefold())]
    chapter = int(match.group("chapter"))
    if not 1 <= chapter <= 150:
        return None
    ref = f"{book} {chapter}"
    if match.group("verse"):
        ref += f":{int(match.group('verse'))}"
        if match.group("to"):
            ref += f"-{int(match.group('to'))}"
    return ref


API = "https://bible-api.com"  # tests point this at a fake
SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


async def passage(reference: str, translation: str = "webbe") -> dict:
    """Fetch the passage text. Raises BibleError with a readable message."""
    url = f"{API}/{quote(reference)}?translation={quote(translation)}"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(url)
            if response.status_code == 404 and translation != "web":
                diag.debug("bible", f"translation {translation} not available — using WEB")
                return await passage(reference, "web")
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise BibleError(f"I couldn't fetch {reference.title()} ({type(error).__name__}).") from None
    verses = data.get("verses") or []
    if not verses:
        raise BibleError(f"I couldn't find {reference.title()}.")
    # paragraphs of ~5 verses so it reads (and is spoken) naturally, verse numbers as small superscripts
    paragraphs, current = [], []
    for verse in verses:
        text = re.sub(r"\s+", " ", str(verse.get("text", ""))).strip()
        current.append(f"{str(verse.get('verse', '')).translate(SUPERSCRIPT)} {text}")
        if len(current) >= 5 or text.endswith(("”", "’")) and len(current) >= 3:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return {"reference": data.get("reference") or reference.title(),
            "translation": data.get("translation_name") or translation.upper(),
            "text": "\n\n".join(paragraphs)}


class BibleError(Exception):
    """A user-facing error fetching a passage."""
