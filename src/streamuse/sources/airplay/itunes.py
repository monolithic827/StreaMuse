"""Finding a track for Apple Music, and opening it there.

The Apple Music app for Windows is not scriptable - the COM interface died with iTunes - and DACP
carries nothing but playpause, nextitem and previtem, so there is no queue to write to. A `music:`
link only opens the app at the track, measured: it does not start it, and Apple documents no
parameter that would. So a listener's request cannot reach playback on its own; it goes to the host
as a pending request and this is what opens it when they pick it. Search and lookup need no key or
account at all - the iTunes Search API is public.
"""

import os
import re

import aiohttp

from .. import RequestTrack

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
REQUEST_TIMEOUT = 5

TRACK_ID = re.compile(r"^[0-9]{1,20}$")


async def search(query: str, hub) -> RequestTrack | None:
    return _track(await _get(SEARCH_URL, {"term": query, "entity": "song", "limit": "1"}, hub))


async def lookup(track_id: str, hub) -> RequestTrack | None:
    """The panel shows what comes back from here, so the card is the store's own words about the id
    rather than anything the listener typed."""
    if not TRACK_ID.match(track_id):
        return None
    return _track(await _get(LOOKUP_URL, {"id": track_id, "entity": "song"}, hub))


def open_in_app(track_id: str) -> bool:
    if not TRACK_ID.match(track_id):
        return False
    os.startfile(f"music://music.apple.com/us/song/{track_id}")
    return True


async def _get(url: str, params: dict, hub) -> dict | None:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as session:
            async with session.get(url, params=params) as reply:
                if reply.status != 200:
                    hub.warn(f"apple: the iTunes catalogue answered {reply.status}")
                    return None
                # The API answers as text/javascript, so aiohttp will not decode it unasked.
                return await reply.json(content_type=None)
    except aiohttp.ClientError as exc:
        hub.warn(f"apple: could not reach the iTunes catalogue ({exc})")
        return None


def _track(payload: dict | None) -> RequestTrack | None:
    results = (payload or {}).get("results") or []
    if not results:
        return None

    result = results[0]
    return RequestTrack(
        id=str(result.get("trackId") or ""),
        title=result.get("trackName") or "",
        artist=result.get("artistName") or "",
        album=result.get("collectionName") or "",
        artUrl=result.get("artworkUrl100") or "",
    )
