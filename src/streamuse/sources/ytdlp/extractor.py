"""Resolves a YouTube or SoundCloud URL, or a plain search query, to a direct audio stream plus
display metadata, by running yt-dlp.exe and reading the JSON it dumps. It only ever extracts -
ffmpeg, already a dependency, decodes the URL this returns the same way it decodes everything else
in this app.

Both sites hand back a signed CDN URL tied to the request that fetched it, so the headers yt-dlp
used (User-Agent above all) have to travel with it or the CDN answers 403 to ffmpeg.
"""

import asyncio
import json
import subprocess
from dataclasses import dataclass

from ... import jobs
from .. import Rejected

CREATE_NO_WINDOW = 0x08000000

#: A bare query (not a URL) searches YouTube; "scsearch1:" prefixes a query to search SoundCloud
#: instead, same as yt-dlp's own CLI.
DEFAULT_SEARCH = "ytsearch1"

#: Long enough for any ordinary track; a full album, mix or podcast pasted by mistake would otherwise
#: tie up the queue - and hold its whole decode in memory, see cache.py - for its entire length.
MAX_DURATION_SECONDS = 15 * 60


@dataclass(frozen=True)
class TrackInfo:
    title: str
    artist: str
    thumbnail_url: str
    duration: float
    #: The canonical page for this track - stable, unlike stream_url, which is a signed CDN link
    #: tied to the request that resolved it. This is what a request keeps to resolve again later.
    webpage_url: str
    stream_url: str
    http_headers: dict[str, str]


async def extract(yt_dlp_path: str, query: str, cookies_file: str) -> TrackInfo:
    # Only the first entry is ever used, and without --playlist-items every one is fully resolved.
    arguments = ["-J", "-f", "bestaudio/best", "--no-playlist", "--playlist-items", "1",
                 "--default-search", DEFAULT_SEARCH]
    if cookies_file:
        arguments += ["--cookies", cookies_file]
    # "--" so a listener's query starting with a dash is a search term rather than a flag.
    arguments += ["--", query]

    process = await asyncio.create_subprocess_exec(
        yt_dlp_path, *arguments,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    jobs.adopt(process)
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        # yt-dlp's own "ERROR: [extractor] ..." line says why; the exit code is always 1.
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"yt-dlp exited {process.returncode}")

    info = json.loads(stdout)

    entries = info.get("entries")
    if entries is not None:
        info = next(iter(entries), None)
        if info is None:
            raise Rejected("no results")

    # A live stream has no fixed length - ffmpeg would pull from an open-ended HLS manifest instead
    # of a normal file, which the queue's one-track-then-advance model isn't built for.
    if info.get("is_live"):
        raise Rejected(f"'{info.get('title') or query}' is live, not a regular video")

    duration = float(info.get("duration") or 0)
    if duration > MAX_DURATION_SECONDS:
        raise Rejected(
            f"'{info.get('title') or query}' is over {MAX_DURATION_SECONDS // 60} minutes - too long to queue")

    return TrackInfo(
        title=info.get("title") or "",
        artist=info.get("uploader") or info.get("artist") or "",
        thumbnail_url=info.get("thumbnail") or "",
        duration=duration,
        webpage_url=info.get("webpage_url") or query,
        stream_url=info["url"],
        http_headers=info.get("http_headers") or {},
    )
