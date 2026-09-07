"""Finding a track for Apple Music, and handing it back.

The Apple Music app for Windows is not scriptable - the COM interface died with iTunes - and DACP
carries nothing but playpause, nextitem and previtem, so there is no queue to write to. A request
is therefore a `music:` handoff, which starts the track rather than lining it up behind the current
one. The search side needs no key or account at all: the iTunes Search API is public.
"""

import os
import re

import aiohttp

from .. import RequestTrack

SEARCH_URL = "https://itunes.apple.com/search"
REQUEST_TIMEOUT = 5

TRACK_ID = re.compile(r"^[0-9]{1,20}$")


async def search(query: str, hub) -> RequestTrack | None:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as session:
            async with session.get(
                SEARCH_URL, params={"term": query, "entity": "song", "limit": "1"}
            ) as reply:
                if reply.status != 200:
                    hub.warn(f"apple: search failed ({reply.status})")
                    return None
                # The API answers as text/javascript, so aiohttp will not decode it unasked.
                payload = await reply.json(content_type=None)
    except aiohttp.ClientError as exc:
        hub.warn(f"apple: search failed ({exc})")
        return None

    results = payload.get("results") or []
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


def play(track_id: str) -> bool:
    if not TRACK_ID.match(track_id):
        return False
    os.startfile(f"music://music.apple.com/us/song/{track_id}")
    return True
