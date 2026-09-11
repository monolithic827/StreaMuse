"""Resolves a YouTube or SoundCloud URL, or a plain search query, to a direct audio stream plus
display metadata, through yt-dlp's Python API. There is no build of yt-dlp that also does the audio
decode this needs, so only extraction happens here - ffmpeg, already a dependency, decodes the URL
this returns the same way it decodes everything else in this app.

Both sites hand back a signed CDN URL tied to the request that fetched it, so the headers yt-dlp
used (User-Agent above all) have to travel with it or the CDN answers 403 to ffmpeg.
"""

import asyncio
from dataclasses import dataclass

import yt_dlp

#: A bare query (not a URL) searches YouTube; "scsearch1:" prefixes a query to search SoundCloud
#: instead, same as yt-dlp's own CLI.
DEFAULT_SEARCH = "ytsearch1"


class _SilentLogger:
    """quiet/no_warnings still let yt-dlp print a raw ERROR line before raising - the caller already
    turns the same exception into a hub.error, so this drops yt-dlp's own copy instead of leaking
    it straight to the console."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


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


def extract(query: str, cookies_file: str) -> TrackInfo:
    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _SilentLogger(),
        "default_search": DEFAULT_SEARCH,
    }
    if cookies_file:
        options["cookiefile"] = cookies_file

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=False)

    entries = info.get("entries")
    if entries is not None:
        info = next(iter(entries), None)
        if info is None:
            raise LookupError(f"no results for '{query}'")

    return TrackInfo(
        title=info.get("title") or "",
        artist=info.get("uploader") or info.get("artist") or "",
        thumbnail_url=info.get("thumbnail") or "",
        duration=float(info.get("duration") or 0),
        webpage_url=info.get("webpage_url") or query,
        stream_url=info["url"],
        http_headers=info.get("http_headers") or {},
    )


async def extract_async(query: str, cookies_file: str) -> TrackInfo:
    """extract_info is a blocking network call, so this runs it off the loop."""
    return await asyncio.to_thread(extract, query, cookies_file)
