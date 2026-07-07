"""BoardGameGeek lookups for the inventory importer.

Uses BGG's public XML API2 `thing` endpoint. Parsing is split from fetching so the
parser can be unit-tested against saved XML without hitting the network.

Docs: https://boardgamegeek.com/wiki/page/BGG_XML_API2
"""
import asyncio
import os
import re
from xml.etree import ElementTree as ET

import aiohttp  # bundled with discord.py

_ID_RE = re.compile(r"/boardgame(?:expansion)?/(\d+)", re.IGNORECASE)
_THING_URL = "https://boardgamegeek.com/xmlapi2/thing"
_USER_AGENT = "RBGA-Bot/1.0 (+https://github.com/zacclarkson/rbga)"

# BGG locked the XML API behind per-app Bearer tokens in 2025 ("XML APIcalypse").
# Register an app at https://boardgamegeek.com/api and set BGG_API_TOKEN.
BGG_API_TOKEN = os.environ.get("BGG_API_TOKEN")


class BGGNotConfigured(RuntimeError):
    """Raised when no BGG_API_TOKEN is set; BGG now requires one."""


def extract_bgg_id(url: str) -> int | None:
    """Pull the numeric id out of a BGG link, or accept a bare id.

    Handles /boardgame/13/catan, /boardgameexpansion/926/..., trailing paths and
    query strings, and a plain "13".
    """
    if not url:
        return None
    url = url.strip()
    if url.isdigit():
        return int(url)
    m = _ID_RE.search(url)
    return int(m.group(1)) if m else None


def _first(item: ET.Element, tag: str, attr: str = "value") -> str | None:
    el = item.find(tag)
    if el is None:
        return None
    return el.get(attr) if attr else (el.text or None)


def _int(item: ET.Element, tag: str) -> int | None:
    val = _first(item, tag)
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n or None  # BGG uses 0 for "unknown"


def parse_thing(xml: bytes) -> dict | None:
    """Turn a BGG `thing` XML response into a dict of the fields we store.

    Returns None if the response has no item (unknown id).
    """
    root = ET.fromstring(xml)
    item = root.find("item")
    if item is None:
        return None

    title = None
    for name in item.findall("name"):
        if name.get("type") == "primary":
            title = name.get("value")
            break

    publisher = None
    for link in item.findall("link"):
        if link.get("type") == "boardgamepublisher":
            publisher = link.get("value")
            break

    # BGG's category links ("Strategy", "Party Game", ...) become our tags.
    tags = [
        link.get("value")
        for link in item.findall("link")
        if link.get("type") == "boardgamecategory" and link.get("value")
    ]

    return {
        "title": title,
        "publisher": publisher,
        "min_players": _int(item, "minplayers"),
        "max_players": _int(item, "maxplayers"),
        "year": _int(item, "yearpublished"),
        "image": _first(item, "image", attr=None),
        # Small variant; embed thumbnails use this (originals are huge).
        "thumbnail": _first(item, "thumbnail", attr=None),
        "tags": tags or None,
    }


async def fetch_game(bgg_id: int, *, retries: int = 3) -> dict | None:
    """Fetch and parse a game from BGG. Returns None if the id is unknown.

    Requires BGG_API_TOKEN (BGG now rejects anonymous requests with 401). BGG
    occasionally answers 202 ("request queued, retry") and rate-limits bulk
    callers with 429; both get a backoff-and-retry (429 honours Retry-After).
    """
    if not BGG_API_TOKEN:
        raise BGGNotConfigured
    headers = {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {BGG_API_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for attempt in range(retries):
            async with session.get(_THING_URL, params={"id": str(bgg_id)}) as resp:
                if resp.status in (202, 429):
                    try:
                        retry_after = float(resp.headers.get("Retry-After", ""))
                    except ValueError:
                        retry_after = 0.0
                    await asyncio.sleep(max(retry_after, 3.0 * (attempt + 1)))
                    continue
                resp.raise_for_status()
                return parse_thing(await resp.read())
    return None  # gave up after retries
